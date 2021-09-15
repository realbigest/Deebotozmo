import logging
import xml.etree.ElementTree as ET
from typing import Union, Optional

import aiohttp
import stringcase
from aiohttp import ClientResponseError

from deebotozmo.commands import Command, GetCleanLogs
from deebotozmo.constants import CLEAN_ACTION_START, CLEAN_ACTION_PAUSE, CLEAN_ACTION_RESUME, CLEAN_ACTION_STOP, \
    CHARGE_MODE_RETURNING, CLEAN_MODE_SPOT_AREA, CLEAN_MODE_AUTO, CHARGE_MODE_RETURN, CHARGE_MODE_CHARGING, \
    CHARGE_MODE_IDLE, FAN_SPEED_NORMAL, FAN_SPEED_MAX
from deebotozmo.events import FanSpeedEvent, BatteryEvent
from deebotozmo.models import Vacuum, RequestAuth, VacuumState
from deebotozmo.util import sanitize_data

_LOGGER = logging.getLogger(__name__)

CLEAN_MODE_TO_XML = {
    CLEAN_MODE_AUTO: 'auto',
    # CLEAN_MODE_EDGE: 'border',
    # CLEAN_MODE_SPOT: 'spot',
    CLEAN_MODE_SPOT_AREA: 'SpotArea',
    # CLEAN_MODE_SINGLE_ROOM: 'singleroom',
    # CLEAN_MODE_STOP: 'stop'
}

CLEAN_MODE_FROM_XML = {
    'auto': CLEAN_MODE_AUTO,
    # 'border': CLEAN_MODE_EDGE,
    # 'spot': CLEAN_MODE_SPOT,
    'spot_area': CLEAN_MODE_SPOT_AREA,
    # 'singleroom': CLEAN_MODE_SINGLE_ROOM,
    # 'stop': CLEAN_MODE_STOP,
}

CLEAN_ACTION_TO_XML = {
    CLEAN_ACTION_START: 's',
    CLEAN_ACTION_PAUSE: 'p',
    CLEAN_ACTION_RESUME: 'r',
    CLEAN_ACTION_STOP: 'h',
}

CLEAN_ACTION_FROM_XML = {
    's': CLEAN_ACTION_START,
    'p': CLEAN_ACTION_PAUSE,
    'r': CLEAN_ACTION_RESUME,
    'h': CLEAN_ACTION_STOP,
}

CHARGE_MODE_TO_XML = {
    CHARGE_MODE_RETURN: 'go',
    CHARGE_MODE_RETURNING: 'Going',
    CHARGE_MODE_CHARGING: 'SlotCharging',
    CHARGE_MODE_IDLE: 'Idle'
}

CHARGE_MODE_FROM_XML = {
    'going': CHARGE_MODE_RETURNING,
    'slot_charging': CHARGE_MODE_CHARGING,
    'idle': CHARGE_MODE_IDLE
}

FAN_SPEED_TO_XML = {
    FAN_SPEED_NORMAL: 'standard',
    FAN_SPEED_MAX: 'strong'
}

FAN_SPEED_FROM_XML = {
    'standard': FAN_SPEED_NORMAL,
    'strong': FAN_SPEED_MAX
}


class CommandXML:

    class PlaySound(Command):
        def __init__(self, json_cmd: Command):
            sid = json_cmd.args["sid"]
            super().__init__("PlaySound", {
                "sid": str(sid)
            })

    class CleanStart(Command):
        def __init__(self, json_cmd: Command):
            clean_type = json_cmd.args["type"]
            super().__init__("Clean", {
                "clean": {
                    "act": CLEAN_ACTION_TO_XML[CLEAN_ACTION_START],
                    "type": CLEAN_MODE_TO_XML[clean_type]
                }})

    class CleanPause(Command):
        def __init__(self, json_cmd: Command):
            super().__init__("Clean", {
                "clean": {
                    "act": CLEAN_ACTION_TO_XML[CLEAN_ACTION_PAUSE]
                }})

    class CleanResume(Command):
        def __init__(self, json_cmd: Command):
            super().__init__("Clean", {
                "clean": {
                    "act": CLEAN_ACTION_TO_XML[CLEAN_ACTION_RESUME]
                }})

    class CleanStop(Command):
        def __init__(self, json_cmd: Command):
            super().__init__("Clean", {
                "clean": {
                    "act": CLEAN_ACTION_TO_XML[CLEAN_ACTION_STOP]
                }})

    class GetCleanInfo(Command):
        def __init__(self, json_cmd: Command):
            super().__init__('GetCleanState')

    class Charge(Command):
        def __init__(self, json_cmd: Command):
            super().__init__("Charge", {
                "charge": {
                    "type": CHARGE_MODE_TO_XML[CHARGE_MODE_RETURN]
                }})

    class GetChargeState(Command):
        def __init__(self, json_cmd: Command):
            super().__init__('GetChargeState')

    class GetBattery(Command):
        def __init__(self, json_cmd: Command):
            super().__init__('GetBatteryInfo')


class EcovacsSendXML:

    def __init__(
            self,
            session: aiohttp.ClientSession,
            auth: RequestAuth,
            portal_url: str,
            verify_ssl: Union[bool, str],
    ):
        self._session = session
        self._auth = auth
        self.portal_url = portal_url
        self.verify_ssl = verify_ssl

    async def send_command(self, json_cmd: Command, vacuum: Vacuum) -> dict:
        # Get original command class name
        cmd_class = json_cmd.__class__.__name__

        # If we have same command defined for XML payload
        if cmd_class in CommandXML.__dict__:
            # Map called JSON command to XML specific version
            xml_cmd = getattr(CommandXML, cmd_class)(json_cmd)
        else:
            _LOGGER.warning(f"Command {cmd_class} is not supported for this vacbot model at the moment.")
            return {}

        json, base_url = self._get_json_and_url(xml_cmd, vacuum)

        _LOGGER.debug(f"Calling {base_url} with {sanitize_data(json)}")

        try:
            async with self._session.post(
                    base_url, json=json, timeout=60, ssl=self.verify_ssl
            ) as res:
                res.raise_for_status()
                if res.status != 200:
                    _LOGGER.warning(f"Error calling API ({res.status}): {base_url}")
                    return {}

                json = await res.json()
                _LOGGER.debug(f"Got {json}")
                return json
        except ClientResponseError as err:
            if err.status == 502:
                _LOGGER.info("Error calling API (502): Unfortunately the ecovacs api is unreliable. "
                             f"URL was: {base_url}")
            else:
                _LOGGER.warning(f"Error calling API ({err.status}): {base_url}")

        return {}

    def _get_json_and_url(self, command: Command, vacuum: Vacuum) -> (dict, str, str):
        def cmd_to_xml(cmd: Command):
            def listobject_to_xml(tag, conv_object):
                rtnobject = ET.Element(tag)
                if type(conv_object) is dict:
                    for k, v in conv_object.items():
                        rtnobject.set(k, v)
                else:
                    rtnobject.set(tag, conv_object)
                return rtnobject

            ctl = ET.Element('ctl')
            for key, value in cmd.args.items():
                if type(value) is dict:
                    inner = ET.Element(key, value)
                    ctl.append(inner)
                elif type(value) is list:
                    for item in value:
                        ixml = listobject_to_xml(key, item)
                        ctl.append(ixml)
                else:
                    ctl.set(key, value)

            return ctl

        json = {"auth": self._auth}
        base_url = self.portal_url

        if command.name == GetCleanLogs().name:
            json.update({
                "td": command.name,
                "did": vacuum.did,
                "resource": vacuum.resource,
            })

            base_url += f"/lg/log.do"
        else:
            payload_xml = cmd_to_xml(command)

            json.update({
                "cmdName": command.name,
                "payload": ET.tostring(payload_xml).decode(),
                "payloadType": vacuum.payload_type,
                "td": "q",
                "toId": vacuum.did,
                "toRes": vacuum.resource,
                "toType": vacuum.get_class,
            })

            base_url += f"/iot/devmanager.do"

        return json, base_url


class EcovacsHandleXML:
    def __init__(self, vacbot):
        self._vacbot = vacbot

    async def handle(self, event_name: str, event_data, requested: bool = True):
        """
        Handle the given event
        :param event_name: the name of the event or request
        :param event_data: the data of it
        :param requested: True if we manual requested the data (ex. via rest). MQTT -> False
        :return: None
        """

        def _ctl_to_dict_api(action_name, xmlstring):
            def represents_int(stringvar):
                try:
                    int(stringvar)
                    return True
                except ValueError:
                    return False

            xml = ET.fromstring(xmlstring)

            if len(xml) > 0:
                result = xml[0].attrib.copy()
                # Fix for difference in XMPP vs API response
                # Depending on the report will use the tag and add "report" to fit the mold of sucks library
                if xml[0].tag == "clean":
                    result['event'] = "CleanReport"
                elif xml[0].tag == "charge":
                    result['event'] = "ChargeState"
                elif xml[0].tag == "battery":
                    result['event'] = "BatteryInfo"
                else:  # Default back to replacing Get from the api cmdName
                    result['event'] = action_name.replace("Get", "", 1)
            else:
                result = xml.attrib.copy()
                result['event'] = action_name.replace("Get", "", 1)
                if 'ret' in result:  # Handle errors as needed
                    if result['ret'] == 'fail':
                        if action_name == "Charge":  # So far only seen this with Charge, when already docked
                            result['event'] = "ChargeState"

            for key in result:
                if not represents_int(result[key]):  # Fix to handle negative int values
                    result[key] = stringcase.snakecase(result[key])

            return result

        _LOGGER.debug(f"Handle {event_name}: {event_data}")

        if requested:
            if event_data.get("ret") == "ok":
                event_data = event_data.get("resp", event_data)
            else:
                _LOGGER.warning(f"Event {event_name} where ret != \"ok\": {event_data}")
                return

        if not event_data == {}:
            event_data = _ctl_to_dict_api(event_name, event_data)
            if event_data is not None:
                method = '_handle_' + event_data['event']
                if hasattr(self, method):
                    await getattr(self, method)(event_data)
                else:
                    _LOGGER.debug(f"Unknown event: {event_data['event']} with {event_data}")

    async def _handle_clean_report(self, event_data: dict):
        event_type = event_data.get('type')
        try:
            event_type = CLEAN_MODE_FROM_XML[event_type]
            event_status = event_data['st']
            event_status = CLEAN_ACTION_FROM_XML[event_status]

            vacbot_new_status: Optional[VacuumState] = None

            if event_status in (CLEAN_ACTION_START, CLEAN_ACTION_RESUME):
                vacbot_new_status = VacuumState.STATE_CLEANING
            elif self._vacbot.status.state != VacuumState.STATE_DOCKED:
                if event_status == CLEAN_ACTION_STOP:
                    vacbot_new_status = VacuumState.STATE_IDLE
                elif event_status == CLEAN_ACTION_PAUSE:
                    vacbot_new_status = VacuumState.STATE_PAUSED

            if vacbot_new_status:
                self._vacbot._set_state(vacbot_new_status)

        except KeyError:
            _LOGGER.warning(f"Unknown cleaning status '{event_type}'")

        fan = event_data.get('speed')
        try:
            fan = FAN_SPEED_FROM_XML[fan]
            self._vacbot.fanSpeedEvents.notify(FanSpeedEvent(fan))
        except KeyError:
            _LOGGER.warning(f"Unknown fan speed: '{fan}'")

    async def _handle_charge_state(self, event_data):
        event_status = CHARGE_MODE_IDLE

        if 'type' in event_data:
            event_type = event_data['type']
            try:
                event_status = CHARGE_MODE_FROM_XML[event_type]
            except KeyError:
                _LOGGER.warning(f"Unknown charging status '{event_type}'")
        elif 'errno' in event_data:
            # Handle errors
            if event_data['ret'] == 'fail' and event_data['errno'] == '8':
                # Already charging
                event_status = CHARGE_MODE_CHARGING
            else:
                # Fall back to Idle status and log this so we can identify more errors
                event_status = CHARGE_MODE_IDLE
                _LOGGER.warning(f"Unknown charging status '{event_data['errno']}'")

        vacbot_new_status: Optional[VacuumState] = None

        if event_status == CHARGE_MODE_CHARGING:
            vacbot_new_status = VacuumState.STATE_DOCKED
        elif event_status == CHARGE_MODE_RETURNING:
            vacbot_new_status = VacuumState.STATE_RETURNING

        # We have to ignore the idle messages, because all it means is that it's not currently charging,
        # in which case the clean_status is a better indicator of what the vacuum is currently up to.
        if vacbot_new_status:
            self._vacbot._set_state(vacbot_new_status)

    async def _handle_battery_info(self, event_data: dict):
        try:
            self._vacbot.batteryEvents.notify(BatteryEvent(event_data["power"]))
        except ValueError:
            _LOGGER.warning(f"Couldn't parse battery status: {event_data}")
