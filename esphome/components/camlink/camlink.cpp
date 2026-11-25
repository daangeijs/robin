#include "camlink.h"

#include <cerrno>
#include <cstring>

#include <fcntl.h>
#include <lwip/sockets.h>

#include "esphome/components/json/json_util.h"
#include "esphome/core/application.h"
#include "esphome/core/log.h"

namespace esphome::camlink {

static const char *const TAG = "camlink";

// Camera BLE: service 0x8801, write 0x0002 (with response), notify 0x0003.
static const auto SERVICE_UUID = esp32_ble::ESPBTUUID::from_uint16(0x8801);
static const auto WRITE_UUID = esp32_ble::ESPBTUUID::from_uint16(0x0002);
static const auto NOTIFY_UUID = esp32_ble::ESPBTUUID::from_uint16(0x0003);
static const uint8_t WAKE[] = {'B', 'T', '_', 'K', 'E', 'Y', '_', 'O', 'N'};

static const uint32_t WAKE_TIMEOUT_MS = 45000;
static const uint32_t JOIN_TIMEOUT_MS = 40000;
static const uint32_t HOME_TIMEOUT_MS = 40000;

static const uint8_t SLIP_END = 0xC0, SLIP_ESC = 0xDB, SLIP_ESC_END = 0xDC, SLIP_ESC_ESC = 0xDD;
static const size_t MAX_FRAME = 1600;

static uint16_t crc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;  // CRC-16/CCITT-FALSE
  for (size_t i = 0; i < len; i++) {
    crc ^= uint16_t(data[i]) << 8;
    for (int b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

void CamLink::setup() {
  this->parent()->set_auto_connect(false);
  this->rx_.reserve(MAX_FRAME);
  this->status_pref_ = global_preferences->make_preference<std::array<char, STATUS_MAX>>(fnv1_hash("camlink_status"));
  std::array<char, STATUS_MAX> saved{};
  if (this->status_pref_.load(&saved)) {
    saved.back() = '\0';
    this->status_json_ = saved.data();
  }
}

void CamLink::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Camlink:\n"
                "  Camera: %s\n"
                "  Session timeout: %" PRIu32 " s",
                this->parent()->address_str(), this->session_timeout_ms_ / 1000);
}

// ---- serial ---------------------------------------------------------------------------

void CamLink::loop() {
  this->read_serial_();
  const uint32_t now = millis();
  const uint32_t in_state = now - this->state_since_;

  switch (this->state_) {
    case State::IDLE:
      break;
    case State::WAKING:
      if (in_state > WAKE_TIMEOUT_MS)
        this->fail_("ble_timeout");
      break;
    case State::JOINING: {
      if (wifi::global_wifi_component->is_connected()) {
        char buf[wifi::SSID_BUFFER_SIZE];
        if (this->cam_ssid_ == wifi::global_wifi_component->wifi_ssid_to(buf)) {
          this->open_socket_();
          break;
        }
      }
      if (in_state > JOIN_TIMEOUT_MS)
        this->fail_("wifi_timeout");
      break;
    }
    case State::READY:
      this->pump_udp_();
      if (now - this->session_started_ > this->session_timeout_ms_)
        this->end_session_("timeout");
      break;
    case State::STOPPING:
      // Home wifi back, or given up waiting: either way we are idle again.
      if (wifi::global_wifi_component->is_connected() || in_state > HOME_TIMEOUT_MS) {
        this->state_ = State::IDLE;
        this->state_since_ = now;
        this->high_freq_.stop();
      }
      break;
  }
}

void CamLink::read_serial_() {
  uint8_t buf[256];
  size_t avail;
  while ((avail = this->available()) > 0) {
    size_t n = std::min(avail, sizeof(buf));
    if (!this->read_array(buf, n))
      return;
    for (size_t i = 0; i < n; i++) {
      uint8_t c = buf[i];
      if (c == SLIP_END) {
        if (!this->rx_overflow_ && this->rx_.size() >= 3)
          this->handle_frame_(this->rx_.data(), this->rx_.size());
        this->rx_.clear();
        this->rx_escape_ = false;
        this->rx_overflow_ = false;
        continue;
      }
      if (this->rx_escape_) {
        c = c == SLIP_ESC_END ? SLIP_END : c == SLIP_ESC_ESC ? SLIP_ESC : c;
        this->rx_escape_ = false;
      } else if (c == SLIP_ESC) {
        this->rx_escape_ = true;
        continue;
      }
      if (this->rx_.size() >= MAX_FRAME) {
        this->rx_overflow_ = true;
        continue;
      }
      this->rx_.push_back(c);
    }
  }
}

void CamLink::send_frame_(uint8_t type, const uint8_t *payload, size_t len) {
  std::vector<uint8_t> body;
  body.reserve(len + 3);
  body.push_back(type);
  body.insert(body.end(), payload, payload + len);
  uint16_t crc = crc16(body.data(), body.size());
  body.push_back(crc >> 8);
  body.push_back(crc & 0xFF);

  std::vector<uint8_t> out;
  out.reserve(body.size() * 2 + 2);
  out.push_back(SLIP_END);
  for (uint8_t c : body) {
    if (c == SLIP_END) {
      out.push_back(SLIP_ESC);
      out.push_back(SLIP_ESC_END);
    } else if (c == SLIP_ESC) {
      out.push_back(SLIP_ESC);
      out.push_back(SLIP_ESC_ESC);
    } else {
      out.push_back(c);
    }
  }
  out.push_back(SLIP_END);
  this->write_array(out.data(), out.size());
}

void CamLink::handle_frame_(const uint8_t *data, size_t len) {
  uint16_t want = (uint16_t(data[len - 2]) << 8) | data[len - 1];
  if (crc16(data, len - 2) != want) {
    ESP_LOGW(TAG, "Dropped frame with bad crc (%u bytes)", (unsigned) len);
    return;
  }
  const uint8_t type = data[0];
  const uint8_t *payload = data + 1;
  const size_t plen = len - 3;

  switch (type) {
    case PING: {
      // State plus what the ESP knows about the camera between sessions.
      char msg[192];
      int wifi_rssi = wifi::global_wifi_component->is_connected() ? wifi::global_wifi_component->wifi_rssi() : 0;
      if (this->camera_seen_ != 0) {
        snprintf(msg, sizeof(msg),
                 "{\"state\":\"%s\",\"uptime\":%" PRIu32 ",\"camera\":\"%s\",\"rssi\":%d,\"seen_s\":%" PRIu32
                 ",\"wifi_rssi\":%d}",
                 this->state_text().c_str(), millis() / 1000, this->parent()->address_str(), (int) this->camera_rssi_,
                 (millis() - this->camera_seen_) / 1000, wifi_rssi);
      } else {
        snprintf(msg, sizeof(msg),
                 "{\"state\":\"%s\",\"uptime\":%" PRIu32 ",\"camera\":\"%s\",\"wifi_rssi\":%d}",
                 this->state_text().c_str(), millis() / 1000, this->parent()->address_str(), wifi_rssi);
      }
      this->send_frame_(PONG, std::string(msg));
      break;
    }
    case START:
      this->start_session_();
      break;
    case UDP_SEND: {
      if (this->state_ != State::READY || this->sock_ < 0 || plen < 6)
        break;
      struct sockaddr_in to {};
      to.sin_family = AF_INET;
      memcpy(&to.sin_addr.s_addr, payload, 4);
      to.sin_port = htons((uint16_t(payload[4]) << 8) | payload[5]);
      if (sendto(this->sock_, payload + 6, plen - 6, 0, reinterpret_cast<sockaddr *>(&to), sizeof(to)) < 0)
        ESP_LOGV(TAG, "sendto failed: %d", errno);
      break;
    }
    case STOP:
      if (this->state_ == State::IDLE || this->state_ == State::STOPPING) {
        this->send_frame_(STOPPED, std::string("{\"reason\":\"requested\"}"));
      } else {
        this->end_session_("requested");
      }
      break;
    case STATUS:
      if (plen >= STATUS_MAX) {
        ESP_LOGW(TAG, "Status too long (%u bytes), ignored", (unsigned) plen);
        break;
      }
      this->status_json_.assign(reinterpret_cast<const char *>(payload), plen);
      {
        std::array<char, STATUS_MAX> save{};
        memcpy(save.data(), payload, plen);
        this->status_pref_.save(&save);
      }
      ESP_LOGI(TAG, "Status from Pi: %s", this->status_json_.c_str());
      break;
    default:
      ESP_LOGW(TAG, "Unknown frame type 0x%02X", type);
  }
}

// ---- session --------------------------------------------------------------------------

void CamLink::start_session_() {
  if (this->state_ != State::IDLE) {
    this->send_frame_(FAIL, std::string("{\"reason\":\"busy\"}"));
    return;
  }
  ESP_LOGI(TAG, "Session start: waking camera");
  this->high_freq_.start();
  this->state_ = State::WAKING;
  this->state_since_ = millis();
  this->session_started_ = millis();
  this->ble_reply_.clear();
  this->cam_ssid_.clear();
  this->cam_password_.clear();
  this->write_handle_ = this->notify_handle_ = 0;
  // The camera uses a random static address (top two bits 11). Connecting directly, without
  // the scanner having handed over the advertisement, the client would assume a public one.
  if ((this->parent()->get_remote_bda()[0] & 0xC0) == 0xC0)
    this->parent()->set_remote_addr_type(BLE_ADDR_TYPE_RANDOM);
  this->parent()->connect();
}

void CamLink::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                  esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto *wr = this->parent()->get_characteristic(SERVICE_UUID, WRITE_UUID);
      auto *nt = this->parent()->get_characteristic(SERVICE_UUID, NOTIFY_UUID);
      if (wr == nullptr || nt == nullptr) {
        ESP_LOGW(TAG, "Camera service 0x8801 not found");
        if (this->state_ == State::WAKING)
          this->fail_("no_service");
        break;
      }
      this->write_handle_ = wr->handle;
      this->notify_handle_ = nt->handle;
      this->parent()->register_for_notify(this->notify_handle_);
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
      if (param->reg_for_notify.handle != this->notify_handle_)
        break;
      this->node_state = esp32_ble_tracker::ClientState::ESTABLISHED;
      // Give the base class a moment to switch notifications on (CCCD), then wake.
      this->set_timeout("wake", 300, [this]() {
        if (this->state_ != State::WAKING)
          return;
        auto err = esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(),
                                            this->write_handle_, sizeof(WAKE), const_cast<uint8_t *>(WAKE),
                                            ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
        if (err != ESP_OK)
          this->fail_("ble_write");
      });
      break;
    }
    case ESP_GATTC_NOTIFY_EVT: {
      if (param->notify.handle != this->notify_handle_ || this->state_ != State::WAKING)
        break;
      this->ble_reply_.insert(this->ble_reply_.end(), param->notify.value,
                              param->notify.value + param->notify.value_len);
      if (memchr(param->notify.value, '}', param->notify.value_len) != nullptr)
        this->got_credentials_();
      break;
    }
    case ESP_GATTC_CLOSE_EVT:
    case ESP_GATTC_DISCONNECT_EVT:
      this->write_handle_ = this->notify_handle_ = 0;
      if (this->state_ == State::WAKING)
        this->fail_("ble_disconnected");
      else if (this->state_ == State::JOINING || this->state_ == State::READY)
        this->end_session_("camera_disconnected");  // the hotspot is gone with the link
      break;
    default:
      break;
  }
}

void CamLink::got_credentials_() {
  // Reply: 8-byte header, then {"ret":0,"ssid":...,"bssid":...,"pwd":...}
  auto *start = static_cast<uint8_t *>(memchr(this->ble_reply_.data(), '{', this->ble_reply_.size()));
  if (start == nullptr) {
    this->fail_("ble_reply");
    return;
  }
  size_t len = this->ble_reply_.data() + this->ble_reply_.size() - start;
  bool ok = json::parse_json(start, len, [this](JsonObject root) -> bool {
    if ((root["ret"] | -1) != 0)
      return false;
    this->cam_ssid_ = root["ssid"] | "";
    this->cam_password_ = root["pwd"] | "";
    return !this->cam_ssid_.empty();
  });
  if (!ok) {
    this->fail_("ble_reply");
    return;
  }
  ESP_LOGI(TAG, "Camera hotspot %s, joining", this->cam_ssid_.c_str());
  wifi::WiFiAP ap;
  ap.set_ssid(this->cam_ssid_);
  ap.set_password(this->cam_password_);
  wifi::global_wifi_component->disable();
  wifi::global_wifi_component->set_sta(ap);
  wifi::global_wifi_component->enable();
  this->on_camera_wifi_ = true;
  this->state_ = State::JOINING;
  this->state_since_ = millis();
}

void CamLink::open_socket_() {
  this->sock_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->sock_ < 0) {
    this->fail_("socket");
    return;
  }
  int yes = 1;
  setsockopt(this->sock_, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes));
  int rcvbuf = 32768;
  setsockopt(this->sock_, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
  fcntl(this->sock_, F_SETFL, fcntl(this->sock_, F_GETFL, 0) | O_NONBLOCK);
  struct sockaddr_in me {};
  me.sin_family = AF_INET;
  me.sin_addr.s_addr = htonl(INADDR_ANY);
  me.sin_port = 0;
  bind(this->sock_, reinterpret_cast<sockaddr *>(&me), sizeof(me));

  char ip[network::IP_ADDRESS_BUFFER_SIZE] = "";
  auto ips = wifi::global_wifi_component->get_ip_addresses();
  if (ips[0].is_set())
    ips[0].str_to(ip);
  char msg[160];
  snprintf(msg, sizeof(msg), "{\"ssid\":\"%s\",\"ip\":\"%s\"}", this->cam_ssid_.c_str(), ip);
  ESP_LOGI(TAG, "On camera hotspot as %s, ready", ip);
  this->state_ = State::READY;
  this->state_since_ = millis();
  this->send_frame_(READY, std::string(msg));
}

void CamLink::pump_udp_() {
  uint8_t buf[1506];
  for (int i = 0; i < 16; i++) {  // bounded, so serial and BLE keep getting their turn
    struct sockaddr_in from {};
    socklen_t flen = sizeof(from);
    int n = recvfrom(this->sock_, buf + 6, sizeof(buf) - 6, 0, reinterpret_cast<sockaddr *>(&from), &flen);
    if (n <= 0)
      return;
    memcpy(buf, &from.sin_addr.s_addr, 4);
    uint16_t port = ntohs(from.sin_port);
    buf[4] = port >> 8;
    buf[5] = port & 0xFF;
    this->send_frame_(UDP_RECV, buf, n + 6);
  }
}

void CamLink::end_session_(const char *reason) {
  ESP_LOGI(TAG, "Session end (%s)", reason);
  if (this->sock_ >= 0) {
    close(this->sock_);
    this->sock_ = -1;
  }
  this->parent()->disconnect();
  this->go_home_();
  char msg[64];
  snprintf(msg, sizeof(msg), "{\"reason\":\"%s\"}", reason);
  this->send_frame_(STOPPED, std::string(msg));
}

void CamLink::fail_(const char *reason) {
  ESP_LOGW(TAG, "Session failed: %s", reason);
  this->cancel_timeout("wake");
  if (this->sock_ >= 0) {
    close(this->sock_);
    this->sock_ = -1;
  }
  this->parent()->disconnect();
  this->go_home_();
  char msg[64];
  snprintf(msg, sizeof(msg), "{\"reason\":\"%s\"}", reason);
  this->send_frame_(FAIL, std::string(msg));
}

void CamLink::go_home_() {
  if (this->on_camera_wifi_) {
    wifi::WiFiAP home;
    home.set_ssid(this->home_ssid_);
    home.set_password(this->home_password_);
    wifi::global_wifi_component->disable();
    wifi::global_wifi_component->set_sta(home);
    wifi::global_wifi_component->enable();
    this->on_camera_wifi_ = false;
  }
  this->state_ = State::STOPPING;
  this->state_since_ = millis();
}

// ---- status for Home Assistant --------------------------------------------------------

float CamLink::status_number(const char *key) const {
  if (this->status_json_.empty())
    return NAN;
  float out = NAN;
  json::parse_json(this->status_json_, [&out, key](JsonObject root) -> bool {
    if (root[key].is<float>())
      out = root[key].as<float>();
    return true;
  });
  return out;
}

int CamLink::status_flag(const char *key) const {
  if (this->status_json_.empty())
    return -1;
  int out = -1;
  json::parse_json(this->status_json_, [&out, key](JsonObject root) -> bool {
    if (root[key].is<bool>())
      out = root[key].as<bool>() ? 1 : 0;
    return true;
  });
  return out;
}

std::string CamLink::status_text(const char *key) const {
  std::string out;
  if (this->status_json_.empty())
    return out;
  json::parse_json(this->status_json_, [&out, key](JsonObject root) -> bool {
    if (root[key].is<const char *>())
      out = root[key].as<const char *>();
    return true;
  });
  return out;
}

std::string CamLink::state_text() const {
  switch (this->state_) {
    case State::IDLE:
      return "idle";
    case State::WAKING:
      return "waking";
    case State::JOINING:
      return "joining";
    case State::READY:
      return "ready";
    case State::STOPPING:
      return "stopping";
  }
  return "unknown";
}

}  // namespace esphome::camlink
