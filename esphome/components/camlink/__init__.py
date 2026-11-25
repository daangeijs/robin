"""camlink: bridge between the trail camera and the Pi on the USB serial line.

Wakes the camera over BLE, joins its hotspot and passes UDP packets to and from
the Pi in SLIP frames. The camera protocol itself lives on the Pi. Frame format:
docs/serial-link.md.
"""

import esphome.codegen as cg
from esphome.components import ble_client, uart
import esphome.config_validation as cv
from esphome.const import CONF_ID

DEPENDENCIES = ["uart", "ble_client", "wifi", "esp32"]
AUTO_LOAD = ["json"]

CONF_HOME_SSID = "home_ssid"
CONF_HOME_PASSWORD = "home_password"
CONF_SESSION_TIMEOUT = "session_timeout"

camlink_ns = cg.esphome_ns.namespace("camlink")
CamLink = camlink_ns.class_(
    "CamLink", cg.Component, uart.UARTDevice, ble_client.BLEClientNode
)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(CamLink),
            cv.Required(CONF_HOME_SSID): cv.string,
            cv.Required(CONF_HOME_PASSWORD): cv.string,
            cv.Optional(
                CONF_SESSION_TIMEOUT, default="15min"
            ): cv.positive_time_period_milliseconds,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(uart.UART_DEVICE_SCHEMA)
    .extend(ble_client.BLE_CLIENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)
    await ble_client.register_ble_node(var, config)
    cg.add(var.set_home(config[CONF_HOME_SSID], config[CONF_HOME_PASSWORD]))
    cg.add(var.set_session_timeout(config[CONF_SESSION_TIMEOUT]))
