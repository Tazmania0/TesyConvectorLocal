# Home Assistant Tesy integration
[![Type](https://img.shields.io/badge/Type-Custom_Component-orange.svg)](https://github.com/TheByteStuff/RemoteSyslog_Service)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

This custom integration allows you to control your Tesy Convector directly from Home Assistant. It provides seamless control of the convector's heating modes and target temperature.
## Features
- HVAC Modes: Switch between Auto, Heat, and Off modes.
- Target Temperature Adjustment: Easily set your desired temperature.
- Supports External Temperature Sensors: Integrate a separate Home Assistant sensor to track temperature.
- Support for convector temperature correction
- Local API Communication: Utilizes the local API, ensuring fast and secure control without relying on cloud services.
- add "set_opened_window" service to set opened window status
- Smart window open detection:
    * Based on temperature drop rate & recovery
    * Customizable hysteresis delay to avoid toggling
    * Binary_sensor for window open status

## Tested with:
- Tesy Convector CN06AS

## Installation

### Via HACS
* Add this repo as a ["Custom repository"](https://hacs.xyz/docs/faq/custom_repositories/) with type "Integration"
* Click "Install" in the new "Tesy" card in HACS.
* Install
* Restart Home Assistant
* Click Add Integration and choose Tesy, follow the configuration flow

### Manual Installation (not recommended)
* Copy the entire `custom_components/tesy_convector_local/` directory to your server's `<config>/custom_components` directory
* Restart Home Assistant

### set_opened_window service usage
    service: tesy_convector_local.set_opened_window
    data:
      entity_id: climate.tesy_convector_cn06as
      status: "on"

