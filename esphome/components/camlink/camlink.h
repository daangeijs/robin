#pragma once

#include <cmath>
#include <string>
#include <vector>

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/uart/uart.h"
#include "esphome/components/wifi/wifi_component.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/preferences.h"

#include <array>

namespace esphome::camlink {

// Frame types, see docs/serial-link.md.
enum FrameType : uint8_t {
  PING = 0x01,
  START = 0x02,
  UDP_SEND = 0x03,
  STOP = 0x04,
  STATUS = 0x05,
  PONG = 0x81,
  READY = 0x82,
  FAIL = 0x83,
  STOPPED = 0x84,
  UDP_RECV = 0x85,
};

enum class State : uint8_t { IDLE, WAKING, JOINING, READY, STOPPING };

class CamLink : public Component, public uart::UARTDevice, public ble_client::BLEClientNode {
 public:
  void set_home(const std::string &ssid, const std::string &password) {
    this->home_ssid_ = ssid;
    this->home_password_ = password;
  }
  void set_session_timeout(uint32_t ms) { this->session_timeout_ms_ = ms; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

  // Last status the Pi sent, for the template sensors in the YAML.
  float status_number(const char *key) const;
  int status_flag(const char *key) const;  // 1, 0, or -1 when unknown
  std::string status_text(const char *key) const;
  std::string state_text() const;
  // Fed by the ble_rssi sensor in the YAML: the camera's advertisements, heard all the time.
  void set_camera_rssi(float rssi) {
    if (std::isnan(rssi))  // the sensor's "not heard" between scans, not an advertisement
      return;
    this->camera_rssi_ = rssi;
    this->camera_seen_ = millis();
  }

 protected:
  // serial
  void read_serial_();
  void handle_frame_(const uint8_t *data, size_t len);
  void send_frame_(uint8_t type, const uint8_t *payload, size_t len);
  void send_frame_(uint8_t type, const std::string &payload) {
    this->send_frame_(type, reinterpret_cast<const uint8_t *>(payload.data()), payload.size());
  }

  // session
  void start_session_();
  void got_credentials_();
  void open_socket_();
  void pump_udp_();
  void end_session_(const char *reason);
  void fail_(const char *reason);
  void go_home_();

  std::string home_ssid_;
  std::string home_password_;
  uint32_t session_timeout_ms_{15 * 60 * 1000};

  State state_{State::IDLE};
  uint32_t state_since_{0};
  uint32_t session_started_{0};
  bool on_camera_wifi_{false};

  // BLE
  uint16_t write_handle_{0};
  uint16_t notify_handle_{0};
  std::vector<uint8_t> ble_reply_;
  std::string cam_ssid_;
  std::string cam_password_;

  // UDP
  int sock_{-1};

  // SLIP receive state
  std::vector<uint8_t> rx_;
  bool rx_escape_{false};
  bool rx_overflow_{false};

  float camera_rssi_{NAN};
  uint32_t camera_seen_{0};

  std::string status_json_;
  // Last status survives a reboot, so Home Assistant keeps the last known values.
  static constexpr size_t STATUS_MAX = 1280;
  ESPPreferenceObject status_pref_;
  HighFrequencyLoopRequester high_freq_;
};

}  // namespace esphome::camlink
