# -*- coding: future_fstrings -*-
# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import platform
import os

from telethon.tl.types import *

from .tgclient import MautrixTelegramClient
from .db import Message as DBMessage
from . import portal as po, puppet as pu, __version__

config = None


class AbstractUser:
    loop = None
    log = None
    db = None
    az = None

    def __init__(self):
        self.connected = False
        self.whitelisted = False
        self.client = None
        self.tgid = None
        self.mxid = None

    def _init_client(self):
        self.log.debug(f"Initializing client for {self.name}")
        device = f"{platform.system()} {platform.release()}"
        sysversion = MautrixTelegramClient.__version__
        self.client = MautrixTelegramClient(self.name,
                                            config["telegram.api_id"],
                                            config["telegram.api_hash"],
                                            loop=self.loop,
                                            app_version=__version__,
                                            system_version=sysversion,
                                            device_model=device)
        self.client.add_update_handler(self._update_catch)

    async def update(self, update):
        return False

    async def post_login(self):
        raise NotImplementedError()

    async def _update_catch(self, update):
        try:
            if not await self.update(update):
                await self._update(update)
        except Exception:
            self.log.exception("Failed to handle Telegram update")

    async def _get_dialogs(self, limit=None):
        dialogs = await self.client.get_dialogs(limit=limit)
        return [dialog.entity for dialog in dialogs if (
            not isinstance(dialog.entity, (User, ChatForbidden, ChannelForbidden))
            and not (isinstance(dialog.entity, Chat)
                     and (dialog.entity.deactivated or dialog.entity.left)))]

    @property
    def name(self):
        raise NotImplementedError()

    @property
    def logged_in(self):
        return self.client and self.client.is_user_authorized()

    @property
    def has_full_access(self):
        return self.logged_in and self.whitelisted

    async def start(self):
        self.connected = await self.client.connect()

    async def ensure_started(self, even_if_no_session=False):
        if not self.whitelisted:
            return self
        elif not self.connected and (even_if_no_session or os.path.exists(f"{self.name}.session")):
            return await self.start()
        return self

    def stop(self):
        self.client.disconnect()
        self.client = None
        self.connected = False

    # region Telegram update handling

    async def _update(self, update):
        if isinstance(update,
                      (UpdateShortChatMessage, UpdateShortMessage, UpdateNewChannelMessage,
                       UpdateNewMessage, UpdateEditMessage, UpdateEditChannelMessage)):
            await self.update_message(update)
        elif isinstance(update, (UpdateChatUserTyping, UpdateUserTyping)):
            await self.update_typing(update)
        elif isinstance(update, UpdateUserStatus):
            await self.update_status(update)
        elif isinstance(update, (UpdateChatAdmins, UpdateChatParticipantAdmin)):
            await self.update_admin(update)
        elif isinstance(update, UpdateChatParticipants):
            portal = po.Portal.get_by_tgid(update.participants.chat_id)
            if portal and portal.mxid:
                await portal.update_telegram_participants(update.participants.participants)
        elif isinstance(update, UpdateChannelPinnedMessage):
            portal = po.Portal.get_by_tgid(update.channel_id)
            if portal and portal.mxid:
                await portal.update_telegram_pin(self, update.id)
        elif isinstance(update, (UpdateUserName, UpdateUserPhoto)):
            await self.update_others_info(update)
        elif isinstance(update, UpdateReadHistoryOutbox):
            await self.update_read_receipt(update)
        else:
            self.log.debug("Unhandled update: %s", update)

    async def update_read_receipt(self, update):
        if not isinstance(update.peer, PeerUser):
            self.log.debug("Unexpected read receipt peer: %s", update.peer)
            return

        portal = po.Portal.get_by_tgid(update.peer.user_id, self.tgid)
        if not portal or not portal.mxid:
            return

        # We check that these are user read receipts, so tg_space is always the user ID.
        message = DBMessage.query.get((update.max_id, self.tgid))
        if not message:
            return

        puppet = pu.Puppet.get(update.peer.user_id)
        await puppet.intent.mark_read(portal.mxid, message.mxid)

    async def update_admin(self, update):
        # TODO duplication not checked
        portal = po.Portal.get_by_tgid(update.chat_id, peer_type="chat")
        if isinstance(update, UpdateChatAdmins):
            await portal.set_telegram_admins_enabled(update.enabled)
        elif isinstance(update, UpdateChatParticipantAdmin):
            await portal.set_telegram_admin(update.user_id)
        else:
            self.log.warning("Unexpected admin status update: %s", update)

    async def update_typing(self, update):
        if isinstance(update, UpdateUserTyping):
            portal = po.Portal.get_by_tgid(update.user_id, self.tgid, "user")
        else:
            portal = po.Portal.get_by_tgid(update.chat_id, peer_type="chat")
        sender = pu.Puppet.get(update.user_id)
        await portal.handle_telegram_typing(sender, update)

    async def update_others_info(self, update):
        # TODO duplication not checked
        puppet = pu.Puppet.get(update.user_id)
        if isinstance(update, UpdateUserName):
            if await puppet.update_displayname(self, update):
                puppet.save()
        elif isinstance(update, UpdateUserPhoto):
            if await puppet.update_avatar(self, update.photo.photo_big):
                puppet.save()
        else:
            self.log.warning("Unexpected other user info update: %s", update)

    async def update_status(self, update):
        puppet = pu.Puppet.get(update.user_id)
        if isinstance(update.status, UserStatusOnline):
            await puppet.intent.set_presence("online")
        elif isinstance(update.status, UserStatusOffline):
            await puppet.intent.set_presence("offline")
        else:
            self.log.warning("Unexpected user status update: %s", update)
        return

    def get_message_details(self, update):
        if isinstance(update, UpdateShortChatMessage):
            portal = po.Portal.get_by_tgid(update.chat_id, peer_type="chat")
            sender = pu.Puppet.get(update.from_id)
        elif isinstance(update, UpdateShortMessage):
            portal = po.Portal.get_by_tgid(update.user_id, self.tgid, "user")
            sender = pu.Puppet.get(self.tgid if update.out else update.user_id)
        elif isinstance(update, (UpdateNewMessage, UpdateNewChannelMessage,
                                 UpdateEditMessage, UpdateEditChannelMessage)):
            update = update.message
            if isinstance(update.to_id, PeerUser) and not update.out:
                portal = po.Portal.get_by_tgid(update.from_id, peer_type="user",
                                               tg_receiver=self.tgid)
            else:
                portal = po.Portal.get_by_entity(update.to_id, receiver_id=self.tgid)
            sender = pu.Puppet.get(update.from_id) if update.from_id else None
        else:
            self.log.warning(
                f"Unexpected message type in User#get_message_details: {type(update)}")
            return update, None, None
        return update, sender, portal

    def update_message(self, original_update):
        update, sender, portal = self.get_message_details(original_update)

        if isinstance(update, MessageService):
            if isinstance(update.action, MessageActionChannelMigrateFrom):
                self.log.debug(f"Ignoring action %s to %s by %d", update.action,
                               portal.tgid_log,
                               sender.id)
                return
            self.log.debug("Handling action %s to %s by %d", update.action, portal.tgid_log,
                           sender.id)
            return portal.handle_telegram_action(self, sender, update)

        user = sender.tgid if sender else "admin"
        if isinstance(original_update, (UpdateEditMessage, UpdateEditChannelMessage)):
            self.log.debug("Handling edit %s to %s by %s", update, portal.tgid_log, user)
            return portal.handle_telegram_edit(self, sender, update)

        self.log.debug("Handling message %s to %s by %s", update, portal.tgid_log, user)
        return portal.handle_telegram_message(self, sender, update)

    # endregion


def init(context):
    global config
    AbstractUser.az, AbstractUser.db, config, AbstractUser.loop, _ = context
