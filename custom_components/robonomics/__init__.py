from __future__ import annotations
from platform import platform

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.auth import auth_manager_from_config, models
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN

from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.service import async_get_all_descriptions

from substrateinterface import Keypair, KeypairType
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import logging
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from robonomicsinterface import Account
from robonomicsinterface.utils import ipfs_32_bytes_to_qm_hash, ipfs_upload_content
import typing as tp
from pinatapy import PinataPy
import os
from ast import literal_eval
import ipfsApi
import time
from datetime import timedelta, datetime

_LOGGER = logging.getLogger(__name__)

from .const import (
    MORALIS_GATEWAY,
    IPFS_GATEWAY,
    CONF_PINATA_PUB,
    CONF_PINATA_SECRET,
    CONF_SUB_OWNER_ADDRESS,
    CONF_ADMIN_SEED,
    DOMAIN,
    CONF_SENDING_TIMEOUT,
    ROBONOMICS,
    PINATA,
    IPFS_API,HANDLE_TIME_CHANGE,
    TIME_CHANGE_UNSUB,
    CONF_CARBON_SERVICE,
    CONF_ENERGY_SENSORS,
    CRUST_GATEWAY,
    LOCAL_GATEWAY,
    HANDLE_LAUNCH,
    DATA_CONFIG_PATH,
    DATA_PATH,
    IPFS_HASH_CONFIG,
    TWIN_ID
)
from .utils import decrypt_message, to_thread, encrypt_for_devices
from .robonomics import Robonomics
import json

manage_users_queue = 0

async def update_listener(hass, entry):
    """
    Handle options update.
    """
    try:
        _LOGGER.debug("Reconfigure Robonomics Integration")
        _LOGGER.debug(f"HASS.data before: {hass.data[DOMAIN]}")
        _LOGGER.debug(f"entry options before: {entry.options}")
        hass.data[DOMAIN][CONF_CARBON_SERVICE] = entry.options[CONF_CARBON_SERVICE]
        if entry.options[CONF_CARBON_SERVICE]:
            hass.data[DOMAIN][CONF_ENERGY_SENSORS] = entry.options[CONF_ENERGY_SENSORS]
        else:
            hass.data[DOMAIN][CONF_ENERGY_SENSORS] = []
        hass.data[DOMAIN][CONF_SENDING_TIMEOUT] = timedelta(minutes=entry.options[CONF_SENDING_TIMEOUT])
        if (CONF_PINATA_PUB in entry.options) and (CONF_PINATA_SECRET in entry.options):
            hass.data[DOMAIN][PINATA] = PinataPy(entry.options[CONF_PINATA_PUB], entry.options[CONF_PINATA_SECRET])
            _LOGGER.debug("Use Pinata to pin files")
        else: 
            hass.data[DOMAIN][PINATA] = None
            _LOGGER.debug("Use local node to pin files")
        hass.data[DOMAIN][TIME_CHANGE_UNSUB]()
        hass.data[DOMAIN][TIME_CHANGE_UNSUB] = async_track_time_interval(hass, hass.data[DOMAIN][HANDLE_TIME_CHANGE], hass.data[DOMAIN][CONF_SENDING_TIMEOUT])
        _LOGGER.debug(f"HASS.data after: {hass.data[DOMAIN]}")
    except Exception as e:
        _LOGGER.error(f"Exception in update_listener: {e}")

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up Robonomics Control from a config entry.
    """
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug(f"Robonomics user control starting set up")
    conf = entry.data
    hass.data[DOMAIN][CONF_CARBON_SERVICE] = conf[CONF_CARBON_SERVICE]
    if hass.data[DOMAIN][CONF_CARBON_SERVICE]:
        hass.data[DOMAIN][CONF_ENERGY_SENSORS] = conf[CONF_ENERGY_SENSORS]
    hass.data[DOMAIN][CONF_SENDING_TIMEOUT] = timedelta(minutes=conf[CONF_SENDING_TIMEOUT])
    _LOGGER.debug(f"Sending interval: {conf[CONF_SENDING_TIMEOUT]} minutes")
    hass.data[DOMAIN][CONF_ADMIN_SEED] = conf[CONF_ADMIN_SEED]
    hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS] = conf[CONF_SUB_OWNER_ADDRESS]

    sub_admin_acc = Account(hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519)
    _LOGGER.debug(f"sub admin: {sub_admin_acc.get_address()}")
    _LOGGER.debug(f"sub owner: {hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS]}")
    hass.data[DOMAIN][ROBONOMICS]: Robonomics = Robonomics(
                            hass,
                            hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS],
                            hass.data[DOMAIN][CONF_ADMIN_SEED]
                            )
    if (CONF_PINATA_PUB in conf) and (CONF_PINATA_SECRET in conf):
        hass.data[DOMAIN][PINATA] = PinataPy(conf[CONF_PINATA_PUB], conf[CONF_PINATA_SECRET])
        _LOGGER.debug("Use Pinata to pin files")
    else: 
        hass.data[DOMAIN][PINATA] = None
        _LOGGER.debug("Use local node to pin files")
    data_path = f"{os.path.expanduser('~')}/{DATA_PATH}"
    if not os.path.isdir(data_path):
        os.mkdir(data_path)
    data_config_path = f"{os.path.expanduser('~')}/{DATA_CONFIG_PATH}"
    if not os.path.isdir(data_config_path):
        os.mkdir(data_config_path)
    if not os.path.exists(f"{data_config_path}/config"):
        with open(f"{data_config_path}/config", "w"):
            pass

    hass.data[DOMAIN][IPFS_API] = ipfsApi.Client('127.0.0.1', 5001)
    hass.data[DOMAIN][HANDLE_LAUNCH] = False

    entry.async_on_unload(entry.add_update_listener(update_listener))

    @to_thread
    def add_to_ipfs(api: ipfsApi.Client, data: str, data_path: str, pinata: PinataPy = None, config: bool = False) -> str:
        """
        Create file with data and pin it to IPFS.
        """
        if config:
            filename = f"{data_path}/config_encrypted"
        else:
            filename = f"{data_path}/data{time.time()}"
        with open(filename, "w") as f:
            f.write(data)
        if pinata is not None:
            try:
                res = pinata.pin_file_to_ipfs(filename)
                if 'IpfsHash' in res:
                    ipfs_hash = res['IpfsHash']
            except Exception as e:
                _LOGGER.error(f"Exception in pinata: {e}")
        _LOGGER.debug(f"IPFS data file: {filename}")
        try:
            res = api.add(filename)
            ipfs_hash_local = res[0]['Hash']
        except Exception as e:
            _LOGGER.error(f"Exception in add data to ipfs witk local node: {e}")
            ipfs_hash_local = None

        # Pin to Crust
        try:
            crust_res = ipfs_upload_content(hass.data[DOMAIN][CONF_ADMIN_SEED], data, pin=True)
        except Exception as e:
            if str(e) == "202":
                _LOGGER.warn(f"202 response from crust")
                crust_res = ['202']
            else:
                _LOGGER.error(f"Exception in add ipfs crust: {e}")
                crust_res = ['error']

        _LOGGER.debug(f"Data pinned to IPFS with hash: {ipfs_hash_local}, crust hash: {crust_res[0]}")
        return ipfs_hash_local

    async def get_provider():
        """
        Returns Home Assistant auth provider
        """
        hass.auth = await auth_manager_from_config(
            hass, [{"type": "homeassistant"}], []
        )
        provider = hass.auth.auth_providers[0]
        await provider.async_initialize()
        return provider

    async def create_user(provider, username: str, password: str) -> None:
        """
        Create user in Home Assistant
        """
        try:
            _LOGGER.debug(f"Start creating user: {username}")
            provider.data.add_auth(username, password)
            creds = models.Credentials(
                auth_provider_type="homeassistant",
                auth_provider_id=None,
                data={"username": username},
                id=username,
                is_new=True,
            )
            resp = await hass.auth.async_get_or_create_user(creds)
            # new_user = await hass.auth.async_create_user(username, ["system-users"])
            # await async_create_person(hass, username, user_id=new_user.id)
            _LOGGER.debug(f"User was created: {username}, password: {password}")
        except Exception as e:
            _LOGGER.error(f"Exception in create user: {e}")

    async def delete_user(provider, username: str) -> None:
        """
        Delete user from Home Assistant
        """
        try:
            _LOGGER.debug(f"Start deleting user {username}")
            provider.data.async_remove_auth(username)
            users = await hass.auth.async_get_users()
            for user in users:
                if user.name == username:
                    await hass.auth.async_remove_user(user)
            # await storage_collection.async_update_item(
            #         person[CONF_ID], {CONF_USER_ID: None}
            #     )
            _LOGGER.debug(f"User was deleted: {username}")
        except Exception as e:
            _LOGGER.error(f"Exception in delete user: {e}")
    
    async def change_password(data):
        """
        Chage password for existing user or create new user
        """
        _LOGGER.debug(f"Start setting password for username {data[0]}")
        provider = await get_provider()
        sender_kp = Keypair(
                ss58_address=data[0], crypto_type=KeypairType.ED25519
            )
        rec_kp = Keypair.create_from_mnemonic(
            hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519
        )
        try:
            message = json.loads(data[2])
        except Exception as e:
            _LOGGER.warning(f"Message in Datalog is in wrong format: {e}\nMessage: {data[2]}")
            return
        if ("admin" in message) and (message["subscription"] == hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS]):
            try:
                password = str(decrypt_message(message["admin"], sender_kp.public_key, rec_kp))
                password = password[2:-1]
                _LOGGER.debug(f"Decrypted password: {password}")
            except Exception as e:
                _LOGGER.error(f"Exception in change password decrypt: {e}")
                return
            try:
                username = data[0].lower()
                users = await hass.auth.async_get_users()
                for user in users:
                    if user.name == username:
                        await delete_user(provider, username)
                await create_user(provider, username, password)
                
            except Exception as e:
                _LOGGER.error(f"Exception in change password: {e}")
                return
            _LOGGER.debug("Restarting...")
            await provider.data.async_save()
            await hass.services.async_call("homeassistant", "restart")

    async def manage_users(data: tp.Tuple(str), add_users: bool = True) -> None:
        """
        Compare users and data from transaction decide what users must be created or deleted
        """
        global manage_users_queue
        manage_users_queue += 1
        my_queue = manage_users_queue
        provider = await get_provider()
        users = provider.data.users
        _LOGGER.debug(f"Begining users: {users}")
        usernames_hass = []
        for user in users:
            try:
                username = user["username"]
                if len(username) == 48 and username[0] == "4":
                    usernames_hass.append(username)
            except Exception as e:
                _LOGGER.error(f"Exception from manage users: {e}")
        devices = data[1]
        if devices is None:
            devices = []
        try:
            if sub_admin_acc.get_address() in devices:
                devices.remove(sub_admin_acc.get_address())
            if hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS] in devices:
                devices.remove(hass.data[DOMAIN][CONF_SUB_OWNER_ADDRESS])
        except Exception as e:
            _LOGGER.error(f"Exception in deleting sub admin and sub owner from devices: {e}")
        hass.data[DOMAIN][ROBONOMICS].devices_list = devices.copy()
        devices = [device.lower() for device in devices]
        users_to_add = list(set(devices) - set(usernames_hass))
        _LOGGER.debug(f"New users: {users_to_add}")
        users_to_delete = list(set(usernames_hass) - set(devices))
        _LOGGER.debug(f"Following users will be deleted: {users_to_delete}")
        rec_kp = Keypair.create_from_mnemonic(hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519)
        created_users = 0
        if add_users:
            for user in users_to_add:
                for device in hass.data[DOMAIN][ROBONOMICS].devices_list:
                    if device.lower() == user:
                        sender_kp = Keypair(ss58_address=device, crypto_type=KeypairType.ED25519)
                        encrypted_password = await hass.data[DOMAIN][ROBONOMICS].find_password(device)
                        if encrypted_password != None:
                            password = str(decrypt_message(encrypted_password, sender_kp.public_key, rec_kp))
                            password = password[2:-1]
                            await create_user(provider, user, password)
                            created_users += 1
                            break
                        else:
                            _LOGGER.debug(f"Password for user {user} wasn't found")
                    
        for user in users_to_delete:
            await delete_user(provider, user)

        if len(users_to_delete) > 0 or created_users > 0:
            await provider.data.async_save()
            _LOGGER.debug(f"Finishing user managment, user list: {provider.data.users}")
            if my_queue < manage_users_queue:
                _LOGGER.debug(f"Another thread will restart homeassistant")
                return
            _LOGGER.debug("Restarting...")
            manage_users_queue = 0
            await hass.services.async_call("homeassistant", "restart")


    def run_launch_command(encrypted_command: str, sender_address: str):
        try:
            if encrypted_command is None:
                _LOGGER.error(f"Can't get command")
                return
        except Exception as e:
            _LOGGER.error(f"Exception in get ipfs command: {e}")
            return
        _LOGGER.debug(f"Got from launch: {encrypted_command}")
        if "platform" in encrypted_command:
            message = literal_eval(encrypted_command)
        else:
            kp_sender = Keypair(ss58_address=sender_address, crypto_type=KeypairType.ED25519)
            sub_admin_kp = Keypair.create_from_mnemonic(
                    hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519
                )
            try:
                decrypted = decrypt_message(encrypted_command, kp_sender.public_key, sub_admin_kp)
            except Exception as e:
                _LOGGER.error(f"Exception in decrypt command: {e}")
                return
            decrypted = str(decrypted)[2:-1]
            _LOGGER.debug(f"Decrypted command: {decrypted}")
            message = literal_eval(decrypted)
        try:
            # domain="light", service="turn_on", service_data={"rgb_color": [30, 30, 230]}, target={"entity_id": "light.shapes_9275"}
            message_entity_id = message["params"]["entity_id"]
            params = message["params"].copy()
            del params["entity_id"]
            if params == {}:
                params = None
            hass.async_create_task(
                hass.services.async_call(
                    domain=message["platform"], 
                    service=message["name"], 
                    service_data=params,
                    target={"entity_id": message_entity_id}
                )
            )
        except Exception as e:
            _LOGGER.error(f"Exception in sending command: {e}")
    
    async def get_request(websession, url: str, sender_address: str) -> None:
        resp = await websession.get(url)
        _LOGGER.debug(f"Responce from {url} is {resp.status}")
        if resp.status == 200:
            if hass.data[DOMAIN][HANDLE_LAUNCH]:
                hass.data[DOMAIN][HANDLE_LAUNCH] = False
                result = await resp.text()
                _LOGGER.debug(f"Result: {result}")
                run_launch_command(result, sender_address)
            
    async def get_ipfs_data(
                ipfs_hash: str, 
                sender_address: str,
                number_of_request: int,
                gateways: tp.List[str] = [CRUST_GATEWAY, 
                                        LOCAL_GATEWAY,
                                        IPFS_GATEWAY,
                                        MORALIS_GATEWAY]   
                ) -> str:
        """
        Get data from IPFS
        """
        if number_of_request > 4:
            return None
        websession = async_create_clientsession(hass)
        try:
            tasks = []
            _LOGGER.debug(f"Request to IPFS number {number_of_request}")
            for gateway in gateways:
                if gateway[-1] != "/":
                    gateway += "/"
                url = f"{gateway}{ipfs_hash}"
                tasks.append(asyncio.create_task(get_request(websession, url, sender_address)))
            for task in tasks:
                await task
        except Exception as e:
            _LOGGER.error(f"Exception in get ipfs: {e}")
            if hass.data[DOMAIN][HANDLE_LAUNCH]:
                await get_ipfs_data(ipfs_hash, sender_address, number_of_request + 1, gateways)

    @callback
    async def handle_launch(data: tp.List[str]) -> None:
        """
        Handle a command from launch transaction
        """
        _LOGGER.debug("Start handle launch")
        hass.data[DOMAIN][HANDLE_LAUNCH] = True
        try:
            ipfs_hash = ipfs_32_bytes_to_qm_hash(data[2])
            response_text = await get_ipfs_data(ipfs_hash, data[0], 0)  # {'platform': 'light', 'name', 'turn_on', 'params': {'entity_id': 'light.lightbulb'}}
        except Exception as e:
            _LOGGER.error(f"Exception in get ipfs command: {e}")
            return
    

    def state_changes_during_period(
        start: datetime.datetime, end: datetime.datetime, entity_id: str
    ) -> list[State]:
        return history.state_changes_during_period(
                hass,
                start,
                end,
                entity_id,
                include_start_time_state=True,
                no_attributes=True,
            ).get(entity_id, [])

    async def get_state_history(entity_id: str) -> tp.List[tp.Tuple[str, str]]:
        """ 
        Get 24 hours history for given entity
        """
        start = datetime.now() - timedelta(hours=24)
        end = datetime.now()
        instance = get_instance(hass)
        states = await instance.async_add_executor_job(
            state_changes_during_period,
            start,
            end,
            entity_id,
        )
        states = states[1:]
        list_states = []
        for state in states:
            list_states.append({"state": state.state, "date": str(state.last_changed)})
        #_LOGGER.debug(f"List of states in history: {list_states}")
        return list_states

    async def get_dashboard_and_services() -> None:
        _LOGGER.debug("Start getting info about dashboard and services")
        entity_registry = er.async_get(hass)
        try:
            descriptions = json.loads(json.dumps(await async_get_all_descriptions(hass)))
        except Exception as e:
            _LOGGER.error(f"Exception in getting descriptions: {e}")
        try:
            services_list = {}
            for entity in entity_registry.entities:
                entity_data = entity_registry.async_get(entity)
                platform = entity_data.entity_id.split('.')[0]
                if platform not in services_list and platform in descriptions:
                    services_list[platform] = descriptions[platform]
            dashboard = hass.data[LOVELACE_DOMAIN]['dashboards'].get(None)
            config_dashboard = await dashboard.async_load(False)
        except Exception as e:
            _LOGGER.error(f"Exception in get services and dashboard: {e}")
        try:
            with open(f"{data_config_path}/config", "r") as f:
                current_config = json.load(f)
        except Exception as e:
            _LOGGER.error(f"Exceprion in json load config: {e}")
            current_config = {}
        try:
            new_config = {"services": services_list, "dashboard": config_dashboard, "twin_id": hass.data[DOMAIN][TWIN_ID]}
            if current_config != new_config or IPFS_HASH_CONFIG not in hass.data[DOMAIN]:
                if current_config != new_config:
                    _LOGGER.debug("Config was changed")
                    with open(f"{data_config_path}/config", "w") as f:
                        json.dump(new_config, f)
                    sender_acc = Account(seed=hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519)
                    sender_kp = sender_acc.keypair
                    devices_list_with_admin = hass.data[DOMAIN][ROBONOMICS].devices_list.copy()
                    devices_list_with_admin.append(sender_acc.get_address())
                    encrypted_data = encrypt_for_devices(str(new_config), sender_kp, devices_list_with_admin)
                else:
                    with open(f"{data_config_path}/config_encrypted") as f:
                        encrypted_data = f.read()
                hass.data[DOMAIN][IPFS_HASH_CONFIG] = await add_to_ipfs(hass.data[DOMAIN][IPFS_API], 
                                                                        encrypted_data, 
                                                                        data_config_path, 
                                                                        pinata=hass.data[DOMAIN][PINATA],
                                                                        config=True)
                _LOGGER.debug(f"New config IPFS hash: {hass.data[DOMAIN][IPFS_HASH_CONFIG]}")
                await hass.data[DOMAIN][ROBONOMICS].set_config_topic(hass.data[DOMAIN][IPFS_HASH_CONFIG], hass.data[DOMAIN][TWIN_ID])
        except Exception as e:
            _LOGGER.error(f"Exception in change config: {e}")

    async def get_states() -> tp.Dict[
        str,
        tp.Dict[str, tp.Union[str, tp.Dict[str, tp.Dict[str, tp.Union[str, float]]]]],
    ]:
        """
        Get info about all entities with 24 hours history
        """ 
        await get_dashboard_and_services()
        registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        devices_data = {'entities': {}}
        data = {}
        used_energy = 0

        for entity in entity_registry.entities:
            entity_data = entity_registry.async_get(entity)
            entity_state = hass.states.get(entity)
            if entity_state != None:
                try:
                    units = str(entity_state.attributes.get("unit_of_measurement"))
                except:
                    units = "None"
                history = await get_state_history(entity_data.entity_id)
                if hass.data[DOMAIN][CONF_CARBON_SERVICE]:
                    if entity_data.entity_id in hass.data[DOMAIN][CONF_ENERGY_SENSORS]:
                        used_energy += float(entity_state.state)
                if entity_data.name != None:
                    entity_name = entity_data.name
                else:
                    entity_name = entity_data.original_name
                entity_info = {
                    "name": entity_name,
                    "units": units,
                    "state": str(entity_state.state),
                    "history": history
                }
                devices_data['entities'][entity_data.entity_id] = entity_info
        if hass.data[DOMAIN][CONF_CARBON_SERVICE]:
            geo = hass.states.get('zone.home')
            devices_data["energy"] = {"energy": used_energy, "geo": (geo.attributes["latitude"], geo.attributes["longitude"])}
        devices_data["twin_id"] = hass.data[DOMAIN][TWIN_ID]
        return devices_data

    # async def handle_datalog(call):
    #     """Handle the service call."""
    #     entity_id = call.data.get(ATTR_ENTITY, DEFAULT_ENTITY)
    #     state = hass.states.get(entity_id)
    #     _LOGGER.debug(f"Datalog service state: {state.state}")
    #     await send_datalog(state.state)

    # hass.services.async_register(DOMAIN, "datalog_send", handle_datalog)

    async def get_and_send_data():
        try:
            sender_acc = Account(seed=hass.data[DOMAIN][CONF_ADMIN_SEED], crypto_type=KeypairType.ED25519)
            sender_kp = sender_acc.keypair
        except Exception as e:
            _LOGGER.error(f"Exception in create keypair during get and senf data: {e}")
        try:
            data = await get_states()
            data = json.dumps(data)
            # with open('/home/homeassistant/ha_test_data', 'w') as f:
            #     f.write(data)
            _LOGGER.debug(f"Got states to send datalog")
            devices_list_with_admin = hass.data[DOMAIN][ROBONOMICS].devices_list.copy()
            devices_list_with_admin.append(sender_acc.get_address())
            data_final = encrypt_for_devices(data, sender_kp, devices_list_with_admin)
            await asyncio.sleep(2)
            ipfs_hash = await add_to_ipfs(hass.data[DOMAIN][IPFS_API], data_final, data_path, pinata=hass.data[DOMAIN][PINATA])
            await hass.data[DOMAIN][ROBONOMICS].send_datalog_states(ipfs_hash)
        except Exception as e:
            _LOGGER.error(f"Exception in get_and_send_data: {e}")


    async def handle_state_changed(event):
        try:
            if (
                    event.data["old_state"] != None
                    and event.data["old_state"].state != "unknown"
                    and event.data["old_state"].state != "unavailable"
                    and event.data["new_state"].state != "unknown"
                    and event.data["new_state"].state != "unavailable"
                    and event.data["entity_id"].split(".")[0] != "sensor"
                    and event.data["old_state"].state != event.data["new_state"].state
                ):
                _LOGGER.debug(f"State changed: {event.data}")
                await get_and_send_data()
        except Exception as e:
            _LOGGER.error(f"Exception in handle_state_changed: {e}")


    async def handle_time_changed(event):
        try:
            _LOGGER.debug(f"Time changed: {event}")
            await get_and_send_data()
        except Exception as e:
            _LOGGER.error(f"Exception in handle_time_changed: {e}")
    
    hass.data[DOMAIN][HANDLE_TIME_CHANGE] = handle_time_changed

    #hass.bus.async_listen("state_changed", handle_state_changed)
    hass.data[DOMAIN][TIME_CHANGE_UNSUB] = async_track_time_interval(hass, hass.data[DOMAIN][HANDLE_TIME_CHANGE], hass.data[DOMAIN][CONF_SENDING_TIMEOUT])
    asyncio.ensure_future(hass.data[DOMAIN][ROBONOMICS].subscribe(handle_launch, manage_users, change_password))

    if TWIN_ID not in hass.data[DOMAIN]:
        try:
            with open(f"{data_config_path}/config", "r") as f:
                current_config = json.load(f)
                _LOGGER.debug(f"Current twin id is {current_config['twin_id']}")
                hass.data[DOMAIN][TWIN_ID] = current_config["twin_id"]
        except Exception as e:
            _LOGGER.debug(f"Can't load config: {e}")
            hass.data[DOMAIN][TWIN_ID] = await hass.data[DOMAIN][ROBONOMICS].create_digital_twin()
            _LOGGER.debug(f"New twin id is {hass.data[DOMAIN][TWIN_ID]}")
 
    hass.states.async_set(f"{DOMAIN}.state", "Online")

    #Checking rws devices to user list correlation
    try:
        start_devices_list = hass.data[DOMAIN][ROBONOMICS].get_devices_list()
        _LOGGER.debug(f"Start devices list is {start_devices_list}")
        hass.async_create_task(manage_users(('0', start_devices_list)))
    except Exception as e:
        print(f"error while getting rws devices list {e}")
    
    await get_and_send_data()
    
    # hass.config_entries.async_setup_platforms(entry, PLATFORMS)
    _LOGGER.debug(f"Robonomics user control successfuly set up")
    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    _LOGGER.debug(f"setup data: {config.get(DOMAIN)}")
    return True


# async def async_unload_entry(
#     hass: core.HomeAssistant, entry: config_entries.ConfigEntry
# ) -> bool:
#     """Unload a config entry."""
#     unload_ok = all(
#         await asyncio.gather(
#             *[hass.config_entries.async_forward_entry_unload(entry, platform)]
#         )
#     )
#     # Remove config entry from domain.
#     if unload_ok:
#         hass.data[DOMAIN].pop(entry.entry_id)

#     return unload_ok


# async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
#     """Unload a config entry."""
#     print(f"hass.data: {hass.data}")
#     print(f"entry id: {entry.entry_id}")
#     print(f"hass domain data: {hass.data[DOMAIN][entry.entry_id]}")
#     component: EntityComponent = hass.data[DOMAIN]
#     return await component.async_unload_entry(entry)

# async def async_remove_entry(hass, entry) -> None:
#     """Handle removal of an entry."""
