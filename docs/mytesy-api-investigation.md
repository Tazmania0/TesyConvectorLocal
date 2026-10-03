# MyTESY heating telemetry investigation

Investigated on 2026-10-03 to determine whether Home Assistant can distinguish
a software heat request from the convector firmware actually heating.

## Evidence and scope

The Android app is MyTESY (`tesy.me`), distinct from the older tesyCloud app
(`com.tesyCloud`). Its official listing is
[Google Play](https://play.google.com/store/apps/details?id=tesy.me).

Inspected the MyTESY 2.7 archive obtained from the
[APK mirror](https://mytesy.apk.watch/2.7), without installing or executing it.
The archive contains `MyTesy-2.7.apk` and configuration split APKs. The base APK
includes a Capacitor application, bundled JavaScript, and a source map with
readable original application sources. The downloaded mirror's publisher
signature has not been independently verified. Matching telemetry handling was
also found in the public, official [cloud client](https://v4.mytesy.com/).

Archive SHA-256:
`03b2ae0a74536e16c167d6eb7f3ab9fdcf934e41cc2d707066a294513d8d6923`.

Bundled `assets/public/static/js/main.ca2be629.js` SHA-256:
`35da508cbc080240436828133e52f7c1e830387c0716cc83e8715ac11f98add2`.

Downloaded binaries and extracted third-party source remain outside this repository.
No cloud account login, broker connection, or device-setting command was made.
Initial local checks used only `getStatus`. Subsequent user-authorized probing
also checked read-only candidate getters and TCP connectivity, as detailed below.

## A separate heating field exists

The convector model handler in `models/cn05/cn05Basic.js`, method
`setTempStatistic`, consumes these telemetry payload fields:

| Field | Application use |
| --- | --- |
| `currentTemp` | Device's reported current temperature |
| `heating` | Heating indicator, with `on` and `off` values |
| `target` | Current thermostat target |
| `timeRemaining` | Remaining mode time |

`pages/phoneApp/DeviceCN05UV.js` displays Heating when the powered-on device's
`heating` field is `on`, and Ready otherwise. `onOff.status` is a separate field.
`models/cn05/cn06uv.js` inherits the CN05UV implementation. The precise cloud model
identifier of the tested physical convector has not yet been retrieved.

This establishes that the app supports device-reported heating telemetry. It
does not independently establish electrical power consumption or telemetry
freshness on the tested device.

## Transport and API calls

The inspected client uses REST at `https://ad.mytesy.com/rest` for account and
device discovery, including `/app-user-login` and `/get-my-devices`. The newer
APK obtains MQTT host, port, username, and password from `/get-app-settings`.
The public web client inspected earlier uses secure WebSockets at
`mqtt.tesy.com:8083`; the optional implementation discovers settings rather than
hardcoding that port or broker credentials. The bundled MQTT.js library defaults
the WebSocket URL path to `/`.

The message path format in `services/Mqtt.js` and `services/MainService.js` is:

```text
v1/<device MAC>/<request or response>/<model>/<device token>/<command>
```

The client subscribes to the selected device's `response/#`,
`getStatusResponse`, `timeRequest/#`, and `pingRequest/#` paths. The
`setTempStatistic` method is an incoming telemetry handler, not evidence that
posting to a local `/setTempStatistic` endpoint reads telemetry. No
`getTempStatistic` getter was found in the inspected client.

For local access, `services/Http.js` uses the same topic-shaped URL path and
`helpers/DeviceRestHelper.js` sends an `application/x-www-form-urlencoded`
POST with a `data` field containing JSON. The configured host is the device's
setup access point at `http://192.168.4.1:80`. Subsequent LAN probing accepted
this route shape with placeholder MAC/token segments; it returned the same
settings as the short `/getStatus` path, without telemetry. This observation
does not establish cloud authentication behavior or behavior on other firmware.

## Local probe results

Probed only the user's identified device, without sending setting commands.
JSON and form-encoded POST requests to `/getStatus` both returned settings.
An app-style form POST to
`/v1/local/request/cn05uv/local/getStatus` returned those same settings.

The candidate read endpoints `/getTempStatistic`, `/getHeatingStatus`,
`/getTemperature`, `/getInfo`, `/getCurrentTemp`, `/getTempAir`, `/getHeating`,
`/getState`, and `/wifiinfo` returned empty payloads. Importantly, the device
still reported HTTP 200 and `code: 200`/`message: OK`; these do not establish that
an endpoint is implemented. `/getSSID` returned settings plus `wifiinfo`, with
no heating or temperature telemetry. GET `/status` returned no useful telemetry.

TCP port 80 was reachable; ports 443, 1883, and 8083 were not reachable in this
check. This is a limited connectivity check, not an exhaustive service scan.
Power, mode, target, and correction were unchanged between the first and final
status reads in the probe batch.

No usable LAN heating-state endpoint was found. This does not prove that no
such endpoint exists.

## Further APK-derived endpoint probes

Searched the application sources bundled in the source map for HTTP calls,
literal command names, dynamically assembled device paths, and setup URLs.
Also searched printable strings in all three base-APK DEX files for TESY
hosts and telemetry endpoint names. The DEX search revealed no additional
TESY-specific HTTP endpoint; it is a string search, not full native-code analysis.

The convector's literal read commands in `helpers/DeviceSettingsHelper.js` are
`getStatus` and `getSSID`; `init.js` additionally issues `ping`. Local setup
pages query `/wifi`. The legacy V2 setup page uses POST `/wscan` on a different
access-point address. The boiler model has a `getDeviceConfig` handler. These
were tested against the user's existing LAN address, along with the relevant
cloud statistics path names as local compatibility checks:

| Request | Result on the tested convector |
| --- | --- |
| POST `/wifi`, form `data` containing `{"command":"wifi"}` | Network-discovery response shape; no heater telemetry |
| POST `/wscan`, empty body | No useful payload |
| POST `/getDeviceConfig`, JSON `{}` | Empty payload |
| POST `/ping`, JSON `{}` | Empty payload |
| App-style form POST ending in `/ping` | Empty payload |
| App-style form POST ending in `/getSSID` | Settings and `wifiinfo`; no heater telemetry |
| App-style form POST with `cn06uv`, ending in `/getStatus` | Same settings as `cn05uv`/short status route |
| App-style form POST ending in `/getDeviceConfig` | Empty payload |
| GET `/get-device-temp-stat` | No useful payload |
| GET `/get-device-power-stat` | No useful payload |
| GET `/get-device-state-stat` | No useful payload |

The three statistics paths are authenticated cloud REST calls in the APK,
not documented LAN endpoints. Their presence in the app does not mean the
convector implements them. All probes returned HTTP 200, including empty
responses, so success codes must not be treated as endpoint support.

The POST `/` setup call writes Wi-Fi/cloud configuration. It and setting,
provisioning, reset, registration, and deletion calls were excluded from probing.
No surrounding network names or device secrets were printed or added to this
repository. The before/after status captures for this second batch were identical.

The remaining positive evidence is the incoming `setTempStatistic` handler
over cloud messaging. It should not be treated as a local getter or probed as
a setting command merely because its name contains `Statistic`.

## Result for this integration

The local `/getStatus` responses captured from the user's device contain
settings but neither `currentTemp`, `heating`, nor `tempAir`. Trying a form-encoded
`getStatus` body also did not reveal those fields. The earlier captures were
identical when the user reported the element stopped and subsequently heating.

Consequently, the existing local response cannot confirm element activity.
`sw_heat_requested` and duty cycle describe commanded phases, and software-mode
`hvac_action` follows that request. A missing internal reading also prevents the
firmware-limit diagnostic from confirming the user's observation.

The next useful check is an authenticated capture restricted to this user's
own device: verify that `setTempStatistic.heating` changes with the observed
element state and identify any further telemetry request from that capture.
Keep account credentials and device tokens out of logs
and source control. If telemetry is cloud-only, consuming it would require an
explicit cloud connection feature; it is not a correction to the existing local
status parser.

An experimental optional implementation now uses authenticated device discovery
and a passive exact-topic subscription. It stores selected-device and broker
credentials, not account credentials. Its message/freshness/lifecycle behavior
is covered by offline tests. No actual account connection or live telemetry
capture has yet been verified; the local probe result above is unchanged.
