# -*- coding: future_fstrings -*-
# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2019 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Awaitable, Any, Dict, List, Iterable, Optional, Pattern, Union, TYPE_CHECKING
from difflib import SequenceMatcher
from enum import Enum
from aiohttp import ServerDisconnectedError
import asyncio
import logging
import re

from telethon.tl.types import (UserProfilePhoto, User, UpdateUserName, PeerUser, TypeInputPeer,
                               InputPeerPhotoFileLocation, UserProfilePhotoEmpty)
from mautrix_appservice import AppService, IntentAPI, IntentError, MatrixRequestError

from .types import MatrixUserID, MatrixRoomID, TelegramID
from .db import Puppet as DBPuppet, PuppetPortal as DBPuppetPortal
from . import util, portal as p

if TYPE_CHECKING:
    from .matrix import MatrixHandler
    from .config import Config
    from .context import Context
    from .abstract_user import AbstractUser

PuppetError = Enum('PuppetError', 'Success OnlyLoginSelf InvalidAccessToken')

config = None  # type: Config


class Puppet:
    log = logging.getLogger("mau.puppet")  # type: logging.Logger
    az = None  # type: AppService
    mx = None  # type: MatrixHandler
    loop = None  # type: asyncio.AbstractEventLoop
    mxid_regex = None  # type: Pattern
    username_template = None  # type: str
    hs_domain = None  # type: str
    cache = {}  # type: Dict[TelegramID, Puppet]
    by_custom_mxid = {}  # type: Dict[str, Puppet]

    def __init__(self,
                 id: TelegramID,
                 access_token: Optional[str] = None,
                 custom_mxid: Optional[MatrixUserID] = None,
                 displayname: Optional[str] = None,
                 displayname_source: Optional[TelegramID] = None,
                 username: Optional[str] = None,
                 first_name: Optional[str] = None,
                 last_name: Optional[str] = None,
                 photo_id: Optional[str] = None,
                 avatar_url: Optional[str] = None,
                 is_bot: bool = False,
                 is_registered: bool = False,
                 disable_updates: bool = False,
                 db_instance: Optional[DBPuppet] = None) -> None:
        self.id = id  # type: TelegramID
        self.access_token = access_token  # type: Optional[str]
        self.custom_mxid = custom_mxid  # type: Optional[MatrixUserID]
        self.default_mxid = self.get_mxid_from_id(self.id)  # type: MatrixUserID

        self.displayname = displayname  # type: Optional[str]
        self.displayname_source = displayname_source  # type: Optional[TelegramID]
        self.username = username  # type: Optional[str]
        self.first_name = first_name  # type: Optional[str]
        self.last_name = last_name  # type: Optional[str]
        self.photo_id = photo_id  # type: Optional[str]
        self.avatar_url = avatar_url  # type: Optional[str]
        self.is_bot = is_bot  # type: bool
        self.is_registered = is_registered  # type: bool
        self.disable_updates = disable_updates  # type: bool
        self._db_instance = db_instance  # type: Optional[DBPuppet]

        self.default_mxid_intent = self.az.intent.user(self.default_mxid)
        self.intent = self._fresh_intent()  # type: IntentAPI
        self.sync_task = None  # type: Optional[asyncio.Future]

        self.cache[id] = self
        if self.custom_mxid:
            self.by_custom_mxid[self.custom_mxid] = self

    @property
    def mxid(self) -> MatrixUserID:
        return self.custom_mxid or self.default_mxid

    @property
    def tgid(self) -> TelegramID:
        return self.id

    @property
    def is_real_user(self) -> bool:
        """ Is True when the puppet is a real Matrix user. """
        return bool(self.custom_mxid and self.access_token)

    @staticmethod
    async def is_logged_in() -> bool:
        """ Is True if the puppet is logged in. """
        return True

    @property
    def plain_displayname(self) -> str:
        tpl = config["bridge.displayname_template"]
        if tpl == "{displayname}":
            # Template has no extra stuff, no need to parse.
            return self.displayname
        regex = re.compile("^" + re.escape(tpl).replace(re.escape("{displayname}"), "(.+?)") + "$")
        match = regex.match(self.displayname)
        return match.group(1) or self.displayname

    def get_input_entity(self, user: 'AbstractUser') -> Awaitable[TypeInputPeer]:
        return user.client.get_input_entity(PeerUser(user_id=self.tgid))

    # region Custom puppet management
    def _fresh_intent(self) -> IntentAPI:
        return (self.az.intent.user(self.custom_mxid, self.access_token)
                if self.is_real_user else self.default_mxid_intent)

    async def switch_mxid(self, access_token: Optional[str],
                          mxid: Optional[MatrixUserID]) -> PuppetError:
        prev_mxid = self.custom_mxid
        self.custom_mxid = mxid
        self.access_token = access_token
        self.intent = self._fresh_intent()

        err = await self.init_custom_mxid()
        if err != PuppetError.Success:
            return err

        try:
            del self.by_custom_mxid[prev_mxid]  # type: ignore
        except KeyError:
            pass
        if self.mxid != self.default_mxid:
            self.by_custom_mxid[self.mxid] = self
            await self.leave_rooms_with_default_user()
        self.save()
        return PuppetError.Success

    async def init_custom_mxid(self) -> PuppetError:
        if not self.is_real_user:
            return PuppetError.Success

        mxid = await self.intent.whoami()
        if not mxid or mxid != self.custom_mxid:
            self.custom_mxid = None
            self.access_token = None
            self.intent = self._fresh_intent()
            if mxid != self.custom_mxid:
                return PuppetError.OnlyLoginSelf
            return PuppetError.InvalidAccessToken
        if config["bridge.sync_with_custom_puppets"]:
            self.sync_task = asyncio.ensure_future(self.sync(), loop=self.loop)
        return PuppetError.Success

    async def leave_rooms_with_default_user(self) -> None:
        for room_id in await self.default_mxid_intent.get_joined_rooms():
            try:
                await self.default_mxid_intent.leave_room(room_id)
                await self.intent.ensure_joined(room_id)
            except (IntentError, MatrixRequestError):
                pass

    def create_sync_filter(self) -> Awaitable[str]:
        return self.intent.client.create_filter(self.custom_mxid, {
            "room": {
                "include_leave": False,
                "state": {
                    "types": []
                },
                "timeline": {
                    "types": [],
                },
                "ephemeral": {
                    "types": ["m.typing", "m.receipt"],
                },
                "account_data": {
                    "types": []
                }
            },
            "account_data": {
                "types": [],
            },
            "presence": {
                "types": ["m.presence"],
                "senders": [self.custom_mxid],
            },
        })

    def filter_events(self, events: List[Dict]) -> List:
        new_events = []
        for event in events:
            evt_type = event.get("type", None)
            event.setdefault("content", {})
            if evt_type == "m.typing":
                is_typing = self.custom_mxid in event["content"].get("user_ids", [])
                event["content"]["user_ids"] = [self.custom_mxid] if is_typing else []
            elif evt_type == "m.receipt":
                val = None
                evt = None
                for event_id in event["content"]:
                    try:
                        val = event["content"][event_id]["m.read"][self.custom_mxid]
                        evt = event_id
                        break
                    except KeyError:
                        pass
                if val and evt:
                    event["content"] = {evt: {"m.read": {
                        self.custom_mxid: val
                    }}}
                else:
                    continue
            new_events.append(event)
        return new_events

    def handle_sync(self, presence: List, ephemeral: Dict) -> None:
        presence_events = [self.mx.try_handle_event(event) for event in presence]

        for room_id, events in ephemeral.items():
            for event in events:
                event["room_id"] = room_id

        ephemeral_events = [self.mx.try_handle_event(event)
                            for events in ephemeral.values()
                            for event in self.filter_events(events)]

        events = ephemeral_events + presence_events  # List[Callable[[int], Awaitable[None]]]
        coro = asyncio.gather(*events, loop=self.loop)
        asyncio.ensure_future(coro, loop=self.loop)

    async def sync(self) -> None:
        try:
            await self._sync()
        except asyncio.CancelledError:
            self.log.info("Syncing cancelled")
        except Exception:
            self.log.exception("Fatal error syncing")

    async def _sync(self) -> None:
        if not self.is_real_user:
            self.log.warning("Called sync() for non-custom puppet.")
            return
        custom_mxid = self.custom_mxid
        access_token_at_start = self.access_token
        errors = 0
        next_batch = None
        filter_id = await self.create_sync_filter()
        self.log.debug(f"Starting syncer for {custom_mxid} with sync filter {filter_id}.")
        while access_token_at_start == self.access_token:
            try:
                sync_resp = await self.intent.client.sync(filter=filter_id, since=next_batch,
                                                          set_presence="offline")  # type: Dict
                errors = 0
                if next_batch is not None:
                    presence = sync_resp.get("presence", {}).get("events", [])  # type: List
                    ephemeral = {room: data.get("ephemeral", {}).get("events", [])
                                 for room, data
                                 in sync_resp.get("rooms", {}).get("join", {}).items()
                                 }  # type: Dict
                    self.handle_sync(presence, ephemeral)
                next_batch = sync_resp.get("next_batch", None)
            except (MatrixRequestError, ServerDisconnectedError) as e:
                wait = min(errors, 11) ** 2
                self.log.warning(f"Syncer for {custom_mxid} errored: {e}. "
                                 f"Waiting for {wait} seconds...")
                errors += 1
                await asyncio.sleep(wait)
        self.log.debug(f"Syncer for custom puppet {custom_mxid} stopped.")

    # endregion
    # region DB conversion

    @property
    def db_instance(self) -> DBPuppet:
        if not self._db_instance:
            self._db_instance = self.new_db_instance()
        return self._db_instance

    def new_db_instance(self) -> DBPuppet:
        return DBPuppet(id=self.id, access_token=self.access_token, custom_mxid=self.custom_mxid,
                        displayname=self.displayname, displayname_source=self.displayname_source,
                        username=self.username, first_name=self.first_name,
                        last_name=self.last_name, photo_id=self.photo_id,
                        avatar_url=self.avatar_url, is_bot=self.is_bot,
                        matrix_registered=self.is_registered, disable_updates=self.disable_updates)

    @classmethod
    def from_db(cls, db_puppet: DBPuppet) -> 'Puppet':
        return Puppet(db_puppet.id, db_puppet.access_token, db_puppet.custom_mxid,
                      db_puppet.displayname, db_puppet.displayname_source, db_puppet.username,
                      db_puppet.first_name, db_puppet.last_name, db_puppet.photo_id,
                      db_puppet.avatar_url, db_puppet.is_bot, db_puppet.matrix_registered,
                      db_puppet.disable_updates, db_instance=db_puppet)

    def save(self) -> None:
        self.db_instance.update(access_token=self.access_token, custom_mxid=self.custom_mxid,
                                displayname=self.displayname,
                                displayname_source=self.displayname_source, username=self.username,
                                first_name=self.first_name, last_name=self.last_name,
                                photo_id=self.photo_id, avatar_url=self.avatar_url,
                                is_bot=self.is_bot, matrix_registered=self.is_registered,
                                disable_updates=self.disable_updates)

    # endregion
    # region Info updating

    def similarity(self, query: str) -> int:
        username_similarity = (SequenceMatcher(None, self.username, query).ratio()
                               if self.username else 0)
        displayname_similarity = (SequenceMatcher(None, self.displayname, query).ratio()
                                  if self.displayname else 0)
        similarity = max(username_similarity, displayname_similarity)
        return int(round(similarity * 100))

    @staticmethod
    def _get_displayname_data(info: User) -> Dict[str, str]:
        return {
            "username": info.username,
            "full name": " ".join([info.first_name or "", info.last_name or ""]).strip(),
            "full name reversed": " ".join([info.first_name or "", info.last_name or ""]).strip(),
            "first name": info.first_name,
            "last name": info.last_name,
        }

    @classmethod
    def get_displayname(cls, info: User, preferences: List[str] = None, template: str = None,
                        data: Dict[str, str] = None) -> str:
        data = data or cls._get_displayname_data(info)
        name = None
        for preference in preferences or config["bridge.displayname_preference"]:
            name = data[preference]
            if name:
                break

        if isinstance(info, User) and info.deleted:
            name = f"Deleted account {info.id}"
        elif not name:
            name = info.id

        return (template or config["bridge.displayname_template"]).format(displayname=name)

    async def update_info(self, source: 'AbstractUser', info: User) -> None:
        if self.disable_updates:
            return
        changed = False

        self.is_bot = info.bot

        changed = await self.update_displayname(source, info) or changed
        if isinstance(info.photo, UserProfilePhoto):
            changed = await self.update_avatar(source, info.photo) or changed

        if changed:
            self.save()

    async def update_displayname(self, source: 'AbstractUser', info: Union[User, UpdateUserName]
                                 ) -> bool:
        update = False
        if ((self.username != info.username or self.first_name != info.first_name
             or self.last_name != info.last_name)):
            self.username = info.username
            self.first_name = info.first_name
            self.last_name = info.last_name
            update = True
        if self.disable_updates:
            return update
        is_main_source = (source.is_relaybot or (self.displayname_source is not None
                                                 and self.displayname_source == source.tgid))
        # No phone -> not in contact list -> can't set custom name -> name is trustworthy
        is_trustworthy_source = isinstance(info, User) and info.phone is None
        if not is_main_source and not is_trustworthy_source:
            return False
        elif isinstance(info, UpdateUserName):
            info = await source.client.get_entity(PeerUser(self.tgid))

        displayname = None
        rich_profile = config["bridge.rich_profile"]
        if not rich_profile:
            displayname = self.get_displayname(info)
            update = self.displayname != displayname
            if update:
                self.displayname = displayname
        if update:
            self.displayname_source = source.tgid
            try:
                await self.update_profile(displayname=displayname, user=info)
            except MatrixRequestError:
                self.log.exception("Failed to set displayname")
            return True
        elif source.is_relaybot or self.displayname_source is None:
            self.displayname_source = source.tgid
            return True
        return False

    async def update_profile_in_room(self, portal: 'p.Portal', user: Optional[User] = None) -> None:
        pupo = DBPuppetPortal.get(self.tgid, portal.tgid)
        await self._set_rich_profile(pupo, portal, user)

    async def _set_rich_profile(self, pupo: Optional[DBPuppetPortal], portal: 'p.Portal',
                                user: Optional[User] = None,
                                dn_data: Optional[Dict[str, str]] = None) -> None:
        local_displayname = portal.get_puppet_displayname(user or self, data=dn_data)
        if not pupo:
            pupo = DBPuppetPortal(self.tgid, portal.tgid, local_displayname)
            pupo.insert()
        elif local_displayname != pupo.displayname:
            pupo.displayname = local_displayname
            pupo.update()
        else:
            return
        await self.default_mxid_intent.send_state_event(portal.mxid, "m.room.member", {
            "displayname": local_displayname,
            "avatar_url": self.avatar_url,
            "net.maunium.telegram.puppet": {
                "user_id": self.tgid,
                "username": self.username,
                "first_name": self.first_name,
                "last_name": self.last_name,
                "is_bot": self.is_bot
            }
        })

    async def update_profile(self, displayname: Optional[str] = None, user: Optional[User] = None,
                             avatar_url: Optional[str] = None) -> None:
        if not config["bridge.rich_profile"]:
            if displayname is not None:
                await self.default_mxid_intent.set_display_name(displayname)
            if avatar_url is not None:
                await self.default_mxid_intent.set_avatar(avatar_url)
            return
        if avatar_url is not None:
            self.avatar_url = avatar_url
        dn_data = self._get_displayname_data(user or self)
        pupos = DBPuppetPortal.all_for_puppet(self.tgid)
        for pupo in pupos:
            portal = p.Portal.get_by_tgid(pupo.portal_id)
            if not portal:
                self.log.debug(f"Deleting PuppetPortal entry {pupo.puppet_id}-{pupo.portal_id}"
                               " for portal that doesn't exist")
                pupo.delete()
                continue
            await self._set_rich_profile(pupo, portal, user, dn_data)

    async def update_avatar(self, source: 'AbstractUser',
                            photo: Union[UserProfilePhoto, UserProfilePhotoEmpty]) -> bool:
        if self.disable_updates:
            return False

        if isinstance(photo, UserProfilePhotoEmpty):
            photo_id = ""
        else:
            photo_id = str(photo.photo_id)
        if self.photo_id != photo_id:
            if not photo_id:
                self.photo_id = ""
                try:
                    await self.update_profile(avatar_url="")
                except MatrixRequestError:
                    self.log.exception("Failed to set avatar")
                    self.photo_id = ""
                return True

            loc = InputPeerPhotoFileLocation(
                peer=await self.get_input_entity(source),
                local_id=photo.photo_big.local_id,
                volume_id=photo.photo_big.volume_id,
                big=True
            )
            file = await util.transfer_file_to_matrix(source.client, self.default_mxid_intent, loc)
            if file:
                self.photo_id = photo_id
                try:
                    await self.update_profile(avatar_url=file.mxc)
                except MatrixRequestError:
                    self.log.exception("Failed to set avatar")
                    self.photo_id = ""
                return True
        return False

    # endregion
    # region Getters

    @classmethod
    def get(cls, tgid: TelegramID, create: bool = True) -> Optional['Puppet']:
        try:
            return cls.cache[tgid]
        except KeyError:
            pass

        puppet = DBPuppet.get_by_tgid(tgid)
        if puppet:
            return cls.from_db(puppet)

        if create:
            puppet = cls(tgid)
            puppet.db_instance.insert()
            return puppet

        return None

    @classmethod
    def get_by_mxid(cls, mxid: MatrixUserID, create: bool = True) -> Optional['Puppet']:
        tgid = cls.get_id_from_mxid(mxid)
        if tgid:
            return cls.get(tgid, create)

        return None

    @classmethod
    def get_by_custom_mxid(cls, mxid: MatrixUserID) -> Optional['Puppet']:
        if not mxid:
            raise ValueError("Matrix ID can't be empty")

        try:
            return cls.by_custom_mxid[mxid]
        except KeyError:
            pass

        puppet = DBPuppet.get_by_custom_mxid(mxid)
        if puppet:
            puppet = cls.from_db(puppet)
            return puppet

        return None

    @classmethod
    def all_with_custom_mxid(cls) -> Iterable['Puppet']:
        return (cls.by_custom_mxid[puppet.mxid]
                if puppet.custom_mxid in cls.by_custom_mxid
                else cls.from_db(puppet)
                for puppet in DBPuppet.all_with_custom_mxid())

    @classmethod
    def get_id_from_mxid(cls, mxid: MatrixUserID) -> Optional[TelegramID]:
        match = cls.mxid_regex.match(mxid)
        if match:
            return TelegramID(int(match.group(1)))
        return None

    @classmethod
    def get_mxid_from_id(cls, tgid: TelegramID) -> MatrixUserID:
        return MatrixUserID(f"@{cls.username_template.format(userid=tgid)}:{cls.hs_domain}")

    @classmethod
    def find_by_username(cls, username: str) -> Optional['Puppet']:
        if not username:
            return None

        for _, puppet in cls.cache.items():
            if puppet.username and puppet.username.lower() == username.lower():
                return puppet

        dbpuppet = DBPuppet.get_by_username(username)
        if dbpuppet:
            return cls.from_db(dbpuppet)

        return None

    @classmethod
    def find_by_displayname(cls, displayname: str) -> Optional['Puppet']:
        if not displayname:
            return None

        for _, puppet in cls.cache.items():
            if puppet.displayname and puppet.displayname == displayname:
                return puppet

        dbpuppet = DBPuppet.get_by_displayname(displayname)
        if dbpuppet:
            return cls.from_db(dbpuppet)

        return None
    # endregion


def init(context: 'Context') -> List[Awaitable[Any]]:  # [None, None, PuppetError]
    global config
    Puppet.az, config, Puppet.loop, _ = context.core
    Puppet.mx = context.mx
    Puppet.username_template = config.get("bridge.username_template", "telegram_{userid}")
    Puppet.hs_domain = config["homeserver"]["domain"]
    Puppet.mxid_regex = re.compile(
        f"@{Puppet.username_template.format(userid='([0-9]+)')}:{Puppet.hs_domain}")
    return [puppet.init_custom_mxid() for puppet in Puppet.all_with_custom_mxid()]
