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
from typing import Type, Dict, Union, Tuple, NamedTuple, List, Optional, Callable, Any, Set
import asyncio
import operator as op

from attr import dataclass
import packaging.version
import semver
import attr

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
        "7": packaging.version.parse("1.37.0rc1"),
        "8": packaging.version.parse("1.40.0rc3"),
    },
    "construct": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": False,
        "7": False,
        "8": False,
    },
    "Dendrite": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": True,
        "7": False,
        "8": False,
    },
    "Conduit": {
        "1": False,
        "2": False,
        "3": False,
        "4": False,
        "5": False,
        "6": True,
        "7": False,
        "8": False,
    },
}

server_order: Dict[str, int] = {
    "Synapse": 100,
    "construct": 50,
    "Conduit": 40,
    "Dendrite": 10,
}

VersionIdentifier = Union[str, packaging.version.Version, semver.VersionInfo]


class ServerInfo(NamedTuple):
    software: str
    version: VersionIdentifier

    @classmethod
    def parse(cls, software: str, version: str) -> 'ServerInfo':
        if software.lower() == "synapse":
            return ServerInfo(software="Synapse",
                              version=packaging.version.parse(version.split(" ")[0]))
        elif software.lower() == "dendrite":
            return ServerInfo(software="Dendrite",
                              version=semver.VersionInfo.parse(version))
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


@dataclass
class Results:
    servers: Dict[str, List[UserID]]
    versions: Dict[str, ServerInfo]
    errors: Dict[str, str]
    event_id: Optional[EventID] = None
    lock: asyncio.Lock = attr.ib(factory=lambda: asyncio.Lock())


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
    tests_in_progress: Set[RoomID] = set()

    async def start(self) -> None:
        self.on_external_config_update()

    @classmethod
    def get_config_class(cls) -> Type[Config]:
        return Config

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
        versions: Dict[str, ServerInfo] = {}
        errors: Dict[str, str] = {}

        async def _test(server_name: str) -> None:
            try:
                versions[server_name] = await asyncio.wait_for(self._test(server_name), timeout=60)
            except TestError as e:
                errors[server_name] = str(e)
            except asyncio.TimeoutError:
                errors[server_name] = "test timed out"
            except Exception:
                errors[server_name] = "internal plugin error"

        await asyncio.gather(*[_test(server) for server in servers.keys()])

        return Results(servers, versions, errors)

    @staticmethod
    def _aggregate_versions(results: Results) -> Dict[ServerInfo, Tuple[int, List[UserID]]]:
        by_version: Dict[ServerInfo, Tuple[int, List[UserID]]] = {}
        for server_name, info in results.versions.items():
            users = results.servers[server_name]
            existing_server_count, existing_users = by_version.get(info, (0, []))
            by_version[info] = (existing_server_count + 1, existing_users + users)
        return dict(sorted(by_version.items(), reverse=True))

    @classmethod
    def _format_results(cls, results: Results) -> str:
        def members(server_name: str) -> str:
            return _pluralize(len(results.servers[server_name]), "member")

        versions_str = ("### Versions\n\n"
                        + "\n".join(f"* {_pluralize(server_count, 'server')} "
                                    f"with {_pluralize(len(users), 'member')} on {info}"
                                    for info, (server_count, users)
                                    in cls._aggregate_versions(results).items()))
        errors_str = ("### Errors\n\n"
                      + "\n".join(f"* {server} ({members(server)}): {error}"
                                  for server, error in results.errors.items())
                      ) if results.errors else ""
        return "\n\n".join((versions_str, errors_str))

    @command.new("servers", aliases=["versions", "server", "version"], require_subcommand=False,
                 help="Check the version of all servers in the room.", arg_fallthrough=False)
    async def servers(self, evt: MessageEvent) -> None:
        if evt.room_id in self.tests_in_progress:
            await evt.reply("There is already a test in progress.")
            return
        self.tests_in_progress.add(evt.room_id)
        try:
            await self._servers(evt)
        finally:
            self.tests_in_progress.remove(evt.room_id)

    async def _servers(self, evt: MessageEvent) -> None:
        event_id = await evt.reply("Loading member list...")
        servers = await self._load_members(evt.room_id)
        user_count = sum(len(users) for users in servers.values())
        await self._edit(evt.room_id, event_id,
                         f"Member list loaded, found {_pluralize(user_count, 'member')} "
                         f"on {_pluralize(len(servers), 'server')}. Now running federation tests")
        results = await self._test_all(servers)
        results.event_id = event_id
        self.caches[evt.room_id] = results
        await self._edit(evt.room_id, event_id, self._format_results(results))

    @servers.subcommand("test", aliases=["check", "version"],
                        help="Test one server, independently of any previous whole-room tests.")
    @command.argument("server", matches=".+", required=True)
    async def test(self, evt: MessageEvent, server: str) -> None:
        await evt.mark_read()

        try:
            version = await asyncio.wait_for(self._test(server), timeout=60)
        except TestError as e:
            await evt.reply(f"Testing {server} failed: {e}")
        except asyncio.TimeoutError:
            await evt.reply(f"Testing {server} failed: test timed out")
        except Exception:
            await evt.reply(f"Testing {server} failed: internal plugin error")
        else:
            await evt.reply(f"{server} is on {version}")

    @servers.subcommand("retest", aliases=["recheck"],
                        help="Re-test one server in the previous results.")
    @command.argument("server", matches=".+", required=True)
    async def retest(self, evt: MessageEvent, server: str) -> None:
        try:
            cache = self.caches[evt.room_id]
        except KeyError:
            await evt.reply("No cached results. Please use `!servers` to test "
                            "all servers in the room first.")
            return
        if server not in cache.servers:
            await evt.reply("That server isn't in the previous results. If the server joined "
                            "recently, you must retest the whole room.")
        try:
            prev_version = cache.versions.pop(server)
            prev_error = None
        except KeyError:
            try:
                prev_error = cache.errors.pop(server)
                prev_version = None
            except KeyError:
                await evt.reply("That server seems to be in the progress of being retested.")
                return

        event_id = await evt.reply(f"Re-testing {server}...")
        new_version: Optional[ServerInfo] = None
        new_error: Optional[str] = None

        try:
            cache.versions[server] = new_version = await asyncio.wait_for(
                self._test(server), timeout=60)
        except TestError as e:
            cache.errors[server] = new_error = str(e)
        except asyncio.TimeoutError:
            cache.errors[server] = new_error = "test timed out"
        except Exception:
            cache.errors[server] = new_error = "internal plugin error"

        if new_error != prev_error or new_version != prev_version:
            async with cache.lock:
                await self._edit(evt.room_id, cache.event_id, self._format_results(cache))

        if new_error is not None:
            cmd_reply_edit = f"Testing {server} failed: {new_error}"
        elif prev_version is not None:
            if prev_version == new_version:
                cmd_reply_edit = f"{server} is still on {prev_version}"
            else:
                if prev_version.software == new_version.software:
                    action = "update" if prev_version < new_version else "downgrade"
                    cmd_reply_edit = (f"{server} {action}d {prev_version.software} "
                                      f"from {prev_version.version} to {new_version.version}")
                else:
                    cmd_reply_edit = f"{server} switched from {prev_version} to {new_version}"
        else:
            cmd_reply_edit = f"{server} is back up and on version {new_version}"

        await self._edit(evt.room_id, event_id, cmd_reply_edit)

    @servers.subcommand("match", help="Show which servers are on a specific version. "
                                      "Operator can be `>`, `<`, `>=`, `<=`, `!=`, `=` or empty.")
    @command.argument("software", matches=".+", required=True)
    @command.argument("operator", required=False, parser=parse_operator)
    @command.argument("version", matches=".+", required=True, pass_raw=True)
    async def match(self, evt: MessageEvent, software: str, operator: Optional[ComparisonOperator],
                    version: str) -> None:
        try:
            cache = self.caches[evt.room_id]
        except KeyError:
            await evt.reply("No results cached. Use `!servers` to test servers first.")
            return
        if not operator:
            operator = op.eq
        try:
            want_info = ServerInfo.parse(software, version)
        except ValueError as e:
            await evt.reply(str(e))
            return
        matches = []

        def antinotify(text: str) -> str:
            return "\ufeff".join(text)

        for server_name, info in cache.versions.items():
            if ((info.software.lower() == want_info.software.lower()
                 and operator(info.version, want_info.version))):
                users = cache.servers[server_name]
                if len(users) == 1:
                    user_list = f"[{antinotify(users[0])}](https://matrix.to/#/{users[0]})"
                elif len(users) == 2:
                    user_list = (f"[{antinotify(users[0])}](https://matrix.to/#/{users[0]}) and "
                                 f"[{antinotify(users[1])}](https://matrix.to/#/{users[1]})")
                else:
                    user_list = (f"[{antinotify(users[0])}](https://matrix.to/#/{users[0]}), "
                                 f"[{antinotify(users[1])}](https://matrix.to/#/{users[1]}) and "
                                 f"{len(users) - 2} others")
                matches.append(f"* {server_name} ({info}) with {user_list}")
        if not matches:
            await evt.reply("No matches :(")
        else:
            await evt.reply("\n".join(matches))
