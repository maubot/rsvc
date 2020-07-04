# rsvc - A maubot plugin that checks the versions of all the servers in a room.
# Copyright (C) 2020 Tulir Asokan
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
from typing import Type, Dict, Union, Tuple, NamedTuple, List, Optional, Callable, Any
import packaging.version
import asyncio
import operator as op

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from mautrix.types import TextMessageEventContent, MessageType, Format, EventID, RoomID, UserID
from mautrix.util import markdown
from maubot import Plugin, MessageEvent
from maubot.handlers import command


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("federation_tester")


class TestError(Exception):
    pass


minimum_version = {
    "Synapse": {
        "1": True,
        "2": packaging.version.parse("0.99.0rc1"),
        "3": packaging.version.parse("0.99.0rc1"),
        "4": packaging.version.parse("0.99.5rc1"),
        "5": packaging.version.parse("1.0.0rc1"),
        "6": packaging.version.parse("1.14.0rc1"),
    },
    "construct": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": False,
    },
    "Dendrite": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": True,
    },
}

server_order: Dict[str, int] = {
    "Synapse": 100,
    "construct": 50,
    "Dendrite": 10,
}

VersionIdentifier = Union[str, packaging.version.Version]


class ServerInfo(NamedTuple):
    software: str
    version: VersionIdentifier

    @classmethod
    def parse(cls, software: str, version: str) -> 'ServerInfo':
        if software.lower() == "synapse":
            return ServerInfo(software="Synapse",
                              version=packaging.version.parse(version.split(" ")[0]))
        else:
            return ServerInfo(software=software, version=version)

    def is_new_enough(self, room_ver: str) -> bool:
        minimum = minimum_version[self.software].get(room_ver, False)
        if isinstance(minimum, bool):
            return minimum
        elif callable(minimum):
            return minimum(self.version)
        else:
            return self.version >= minimum

    def __str__(self) -> str:
        return f"{self.software} {self.version}"

    def __lt__(self, other: 'ServerInfo') -> bool:
        if self.software == other.software:
            return self.version < other.version
        else:
            return server_order.get(self.software, 0) < server_order.get(other.software, 0)


Results = NamedTuple('Results', versions=Dict[ServerInfo, Tuple[int, List[UserID]]],
                     servers=Dict[str, Tuple[ServerInfo, List[UserID]]], errors=Dict[str, str])


def _pluralize(val: int, word: str) -> str:
    if val == 1:
        return f"{val} {word}"
    else:
        return f"{val} {word}s"


ComparisonOperator = Callable[[Any, Any], bool]

op_map: Dict[str, ComparisonOperator] = {
    "=": op.eq,
    "==": op.eq,
    "===": op.eq,
    ">": op.gt,
    ">=": op.ge,
    "<": op.lt,
    "<=": op.le,
    "!=": op.ne,
    "!==": op.ne,
    "≠": op.ne,
}


def parse_operator(val: str) -> ComparisonOperator:
    return op_map.get(val)


class ServerCheckerBot(Plugin):
    caches: Dict[RoomID, Results] = {}

    async def start(self) -> None:
        self.on_external_config_update()

    @classmethod
    def get_config_class(cls) -> Type[Config]:
        return Config

    @command.new("server")
    @command.argument("server", required=True)
    async def server(self, evt: MessageEvent, server: str) -> None:
        pass

    async def _edit(self, room_id: RoomID, event_id: EventID, text: str) -> None:
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=text, format=Format.HTML,
                                          formatted_body=markdown.render(text))
        content.set_edit(event_id)
        await self.client.send_message(room_id, content)

    async def _test(self, server: str) -> ServerInfo:
        self.log.debug(f"Testing {server}")
        resp = await self.http.get(self.config["federation_tester"].format(server=server))
        result = await resp.json()

        if not result["FederationOK"]:
            raise TestError("federation not OK")

        try:
            name = result["Version"]["name"]
            version = result["Version"].get("version", "unknown version")
        except KeyError:
            raise TestError("server not responding to version requests")
        return ServerInfo.parse(name, version)

    async def _load_members(self, room_id: RoomID) -> Dict[str, List[UserID]]:
        users = await self.client.get_joined_members(room_id)
        servers: Dict[str, List[UserID]] = {}
        for user in users:
            _, server = self.client.parse_user_id(user)
            servers.setdefault(server, []).append(user)
        return servers

    async def _test_all(self, servers: Dict[str, List[UserID]]) -> Results:
        by_version: Dict[ServerInfo, Tuple[int, List[UserID]]] = {}
        server_versions: Dict[str, Tuple[ServerInfo, List[UserID]]] = {}
        errors: Dict[str, str] = {}

        async def _test(server_name: str, users: List[UserID]) -> None:
            try:
                result: ServerInfo = await asyncio.wait_for(self._test(server_name), timeout=60)
                existing_server_count, existing_users = by_version.get(result, (0, []))
                by_version[result] = (existing_server_count + 1, existing_users + users)
                server_versions[server_name] = (result, users)
            except TestError as e:
                errors[server_name] = str(e)
            except asyncio.TimeoutError:
                errors[server_name] = "test timed out"
            except Exception:
                errors[server_name] = "internal plugin error"

        await asyncio.gather(*[_test(server, users) for server, users in servers.items()])

        return Results(dict(sorted(by_version.items(), reverse=True)), server_versions, errors)

    @command.new("servers", require_subcommand=False)
    async def servers(self, evt: MessageEvent) -> None:
        event_id = await evt.reply("Loading member list...")
        servers = await self._load_members(evt.room_id)
        user_count = sum(len(users) for users in servers.values())
        await self._edit(evt.room_id, event_id,
                         f"Member list loaded, found {_pluralize(user_count, 'member')} "
                         f"on {_pluralize(len(servers), 'server')}. Now running federation tests")
        results = await self._test_all(servers)
        self.caches[evt.room_id] = results
        by_version, _, errors = results
        versions = ("### Versions\n\n"
                    + "\n".join(f"* {_pluralize(server_count, 'server')} "
                                f"with {_pluralize(len(users), 'member')} on {info}"
                                for info, (server_count, users) in by_version.items()))
        errors = ("### Errors\n\n"
                  + "\n".join(f"* {server} ({_pluralize(len(servers[server]), 'member')}): {error}"
                              for server, error in errors.items())
                  ) if errors else ""
        await self._edit(evt.room_id, event_id, "\n\n".join((versions, errors)))

    @servers.subcommand("match", help="Show which servers are on a specific version")
    @command.argument("software", required=True)
    @command.argument("operator", required=False, parser=parse_operator)
    @command.argument("version", required=True, pass_raw=True)
    async def match(self, evt: MessageEvent, software: str, operator: Optional[ComparisonOperator],
                    version: str) -> None:
        try:
            servers = self.caches[evt.room_id].servers
        except KeyError:
            await evt.reply("No results cached. Use `!servers` to test servers first.")
            return
        if not operator:
            operator = op.eq
        want_info = ServerInfo.parse(software, version)
        matches = []
        for server_name, (info, users) in servers.items():
            if ((info.software.lower() == want_info.software.lower()
                 and operator(info.version, want_info.version))):
                if len(users) == 1:
                    user_list = f"[{users[0]}](https://matrix.to/#/{users[0]})"
                elif len(users) == 2:
                    user_list = (f"[{users[0]}](https://matrix.to/#/{users[0]}) and "
                                 f"[{users[1]}](https://matrix.to/#/{users[1]})")
                else:
                    user_list = (f"[{users[0]}](https://matrix.to/#/{users[0]}), "
                                 f"[{users[1]}](https://matrix.to/#/{users[1]}) and "
                                 f"{len(users) - 2} others")
                matches.append(f"* {server_name} with {user_list}")
        if not matches:
            await evt.reply("No matches :(")
        else:
            await evt.reply("\n".join(matches))
