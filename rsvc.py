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
from __future__ import annotations

from typing import Any, Callable, NamedTuple, Union
import asyncio
import html
import operator as op

from attr import dataclass
import attr
import packaging.version
import semver

from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.types import EventID, Format, MessageType, RoomID, TextMessageEventContent, UserID
from mautrix.util import markdown
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("federation_tester")


class TestError(Exception):
    pass


known_room_versions = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
versions_updated = "2022-08-19"
latest_known_version = {
    "Synapse": packaging.version.parse("1.65.0"),
    "Dendrite": semver.parse("0.9.3"),
    "Conduit": semver.parse("0.4.0"),
}

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
        "9": packaging.version.parse("1.42.0rc2"),
        "10": packaging.version.parse("1.64.0rc1"),
    },
    "construct": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": True,
        "7": True,
        "8": True,
        "9": True,
        "10": False,
    },
    "Dendrite": {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": True,
        "7": semver.parse("0.4.1"),
        "8": semver.parse("0.8.6"),  # actually added in 0.5.1, but only marked as stable in 0.8.6
        "9": semver.parse("0.8.6"),
        "10": semver.parse("0.8.7"),
    },
    "Conduit": {
        "1": False,
        "2": False,
        "3": False,
        "4": False,
        "5": False,  # Conduit's support for room versions below v6 is marked as unstable
        "6": True,
        "7": semver.parse("0.4.0"),
        "8": semver.parse("0.4.0"),
        "9": semver.parse("0.4.0"),
        "10": False,
    },
    "Catalyst": {
        "1": False,
        "2": False,
        "3": False,
        "4": False,
        "5": False,  # Same as Conduit
        "6": True,
        "7": True,
        "8": True,
        "9": True,
        "10": True,
    },
}

server_order: dict[str, int] = {
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
    def parse(cls, software: str, version: str) -> ServerInfo:
        software_lower = software.lower()
        if software_lower == "synapse":
            return ServerInfo(
                software="Synapse",
                version=packaging.version.parse(version.split(" ")[0]),
            )
        elif software_lower == "dendrite":
            return ServerInfo(software="Dendrite", version=semver.VersionInfo.parse(version))
        elif software_lower == "conduit":
            return ServerInfo(software="Conduit", version=semver.VersionInfo.parse(version))
        elif software_lower == "catalyst":
            return ServerInfo(software="Catalyst", version=semver.VersionInfo.parse(version))
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

    @property
    def is_unknown(self) -> bool:
        try:
            return latest_known_version[self.software] < self.version
        except KeyError:
            return False

    def __str__(self) -> str:
        return f"{self.software} {self.version}"

    def __lt__(self, other: ServerInfo) -> bool:
        if self.software == other.software:
            return self.version < other.version
        else:
            return server_order.get(self.software, 0) < server_order.get(other.software, 0)


@dataclass
class Results:
    servers: dict[str, list[UserID]]
    versions: dict[str, ServerInfo]
    errors: dict[str, str]
    event_id: EventID | None = None
    lock: asyncio.Lock = attr.ib(factory=lambda: asyncio.Lock())


def _pluralize(val: int, word: str) -> str:
    if val == 1:
        return f"{val} {word}"
    else:
        return f"{val} {word}s"


ComparisonOperator = Callable[[Any, Any], bool]

op_map: dict[str, ComparisonOperator] = {
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
    caches: dict[RoomID, Results]
    tests_in_progress: dict[RoomID, asyncio.Task]

    async def start(self) -> None:
        self.caches = {}
        self.tests_in_progress = {}
        self.on_external_config_update()

    @classmethod
    def get_config_class(cls) -> type[Config]:
        return Config

    async def _edit(
        self, room_id: RoomID, event_id: EventID, text: str, allow_html: bool = False
    ) -> None:
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE,
            body=text,
            format=Format.HTML,
            formatted_body=markdown.render(text, allow_html=allow_html),
        )
        content.set_edit(event_id)
        await self.client.send_message(room_id, content)

    @staticmethod
    def _parse_error(server: str, result: dict) -> str:
        ipv4_failures = 0
        ipv6_failures = 0
        ipv4_connections = 0
        ipv6_connections = 0
        ipv4_successes = 0
        ipv6_successes = 0
        errors = set()
        errors_with_addr = []
        failed_addresses = 0
        addr: str
        for addr, error in result.get("ConnectionErrors", {}).items():
            if addr.startswith("[") or addr.count(":") > 1:
                ipv6_failures += 1
            else:
                ipv4_failures += 1
        for addr, data in result.get("ConnectionReports", {}).items():
            if addr.startswith("[") or addr.count(":") > 1:
                ipv6_connections += 1
            else:
                ipv4_connections += 1

            def add_err(error: str) -> None:
                nonlocal failed_addresses
                failed_addresses += 1
                if error not in errors:
                    errors.add(error)
                    # if addr.startswith("[") or addr.count(":") > 1:
                    #     ipv6_errors.append((addr, error))
                    # else:
                    errors_with_addr.append((addr, error))

            checks = data.get("Checks") or {}
            if not checks.get("MatchingServerName"):
                got_server = (data.get("Keys") or {}).get("server_name", "undefined")
                add_err(f"mismatching server name, tested: {server}, got: {got_server}")
            elif not checks.get("ValidCertificates"):
                add_err("invalid TLS certificates")
            elif not checks.get("AllChecksOK"):
                add_err("some checks failed")
            else:
                if addr.startswith("[") or addr.count(":") > 1:
                    ipv6_successes += 1
                else:
                    ipv4_successes += 1
        total_ipv4 = ipv4_failures + ipv4_connections
        total_ipv6 = ipv6_failures + ipv6_connections
        msgs = []
        if ipv4_failures + ipv6_failures == total_ipv4 + total_ipv6:
            if total_ipv4 + total_ipv6 == 1:
                msgs.append("Server couldn't be reached")
            else:
                msgs.append("Server couldn't be reached on any address")
        else:
            if ipv4_failures:
                prefix = (
                    f"{ipv4_failures}/{total_ipv4} IPv4 addresses"
                    if total_ipv4 > 1
                    else "IPv4 address"
                )
                msgs.append(f"{prefix} couldn't be reached")
            if ipv6_failures:
                prefix = (
                    f"{ipv6_failures}/{total_ipv6} IPv6 addresses"
                    if total_ipv6 > 1
                    else "IPv6 address"
                )
                msgs.append(f"{prefix} couldn't be reached")
        if errors_with_addr:
            es = "es" if failed_addresses > 1 else ""
            if len(errors_with_addr) == 1:
                _, errors_msg = errors_with_addr[0]
            else:
                errors_msg = ", ".join(f"{addr}: {msg}" for addr, msg in errors_with_addr)
            msgs.append(
                f"{failed_addresses}/{total_ipv4+total_ipv6} address{es} failed the test: "
                f"{errors_msg}"
            )
        suffix = ""
        if ipv4_successes:
            if total_ipv4 == 1:
                suffix = "IPv4 is OK"
            else:
                suffix = f"{ipv4_successes}/{total_ipv4} IPv4 addresses are OK"
        if ipv6_successes:
            if total_ipv6 == 1:
                msg = "IPv6 is OK"
            else:
                msg = f"{ipv6_successes}/{total_ipv6} IPv6 addresses are OK"
            if suffix:
                suffix = f"{suffix} and {msg}"
            else:
                suffix = msg
        if suffix:
            suffix = f" ({suffix})"
        if len(msgs) > 1:
            return f"{', '.join(msgs[0:-1])} and {msgs[-1]}" + suffix
        elif len(msgs) == 1:
            return msgs[0] + suffix
        elif total_ipv4 + total_ipv6 == 0:
            return "No server addresses found"
        else:
            return "federation not OK (unknown error)"

    async def _test(self, server: str) -> ServerInfo:
        self.log.debug(f"Testing {server}")
        resp = await self.http.get(self.config["federation_tester"].format(server=server))
        result = await resp.json()

        if not result["FederationOK"]:
            error_msg = self._parse_error(server, result)
            name = (result.get("Version") or {}).get("name", "")
            version = (result.get("Version") or {}).get("version", "")
            if name and version:
                server_info = ServerInfo.parse(name, version)
                raise TestError(f"{error_msg} // {server_info}")
            raise TestError(error_msg)

        try:
            name = result["Version"]["name"]
            version = result["Version"].get("version", "unknown version")
        except KeyError:
            raise TestError("server not responding to version requests")
        return ServerInfo.parse(name, version)

    async def _load_members(self, room_id: RoomID) -> dict[str, list[UserID]]:
        users = await self.client.get_joined_members(room_id)
        servers: dict[str, list[UserID]] = {}
        for user in users:
            _, server = self.client.parse_user_id(user)
            servers.setdefault(server, []).append(user)
        return servers

    async def _test_all(self, servers: dict[str, list[UserID]]) -> Results:
        versions: dict[str, ServerInfo] = {}
        errors: dict[str, str] = {}

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
    def _aggregate_versions(
        results: Results,
    ) -> dict[ServerInfo, tuple[int, list[UserID]]]:
        by_version: dict[ServerInfo, tuple[int, list[UserID]]] = {}
        for server_name, info in results.versions.items():
            users = results.servers[server_name]
            existing_server_count, existing_users = by_version.get(info, (0, []))
            by_version[info] = (existing_server_count + 1, existing_users + users)
        return dict(sorted(by_version.items(), reverse=True))

    @classmethod
    def _format_results(cls, results: Results) -> str:
        def members(server_name: str) -> str:
            return _pluralize(len(results.servers[server_name]), "member")

        versions_str = "\n".join(
            f"* {_pluralize(server_count, 'server')} "
            f"with {_pluralize(len(users), 'member')} on {info}"
            for info, (server_count, users) in cls._aggregate_versions(results).items()
        )
        versions_str = f"### Versions\n\n{versions_str}"
        errors_str = "\n".join(
            f"* {server} ({members(server)}): {error}" for server, error in results.errors.items()
        )
        errors_str = (
            f"<details><summary>{_pluralize(len(results.errors), 'server')} failed</summary>"
            f"\n\n{errors_str}\n\n</details>"
        )
        if not results.errors:
            return versions_str
        return f"{versions_str}\n\n{errors_str}"

    @command.new(
        "servers",
        aliases=["versions", "server", "version"],
        require_subcommand=False,
        help="Check the version of all servers in the room.",
        arg_fallthrough=False,
    )
    async def servers(self, evt: MessageEvent) -> None:
        if evt.room_id in self.tests_in_progress:
            await evt.reply("There is already a test in progress.")
            return
        await self.test_or_wait(evt)

    async def test_or_wait(self, evt: MessageEvent) -> None:
        if evt.room_id in self.tests_in_progress:
            await self.tests_in_progress[evt.room_id]
            return
        self.tests_in_progress[evt.room_id] = task = asyncio.create_task(self._servers(evt))
        try:
            await task
        finally:
            del self.tests_in_progress[evt.room_id]

    async def cached_or_test(self, evt: MessageEvent) -> Results | None:
        try:
            return self.caches[evt.room_id]
        except KeyError:
            await self.test_or_wait(evt)
            try:
                return self.caches[evt.room_id]
            except KeyError:
                await evt.reply("Cache didn't contain test results even after waiting 😿")
                return None

    async def _servers(self, evt: MessageEvent) -> None:
        event_id = await evt.reply("Loading member list...")
        servers = await self._load_members(evt.room_id)
        user_count = sum(len(users) for users in servers.values())
        await self._edit(
            evt.room_id,
            event_id,
            f"Member list loaded, found {_pluralize(user_count, 'member')} "
            f"on {_pluralize(len(servers), 'server')}. Now running federation tests",
        )
        results = await self._test_all(servers)
        results.event_id = event_id
        self.caches[evt.room_id] = results
        await self._edit(evt.room_id, event_id, self._format_results(results), allow_html=True)

    @servers.subcommand(
        "test",
        aliases=["check", "version"],
        help="Test one server, independently of any previous whole-room tests.",
    )
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

    @servers.subcommand(
        "retest",
        aliases=["recheck"],
        help="Re-test one server in the previous results.",
    )
    @command.argument("server", matches=".+", required=True)
    async def retest(self, evt: MessageEvent, server: str) -> None:
        try:
            cache = self.caches[evt.room_id]
        except KeyError:
            await evt.reply(
                "No cached results. Please use `!servers` to test all servers in the room first."
            )
            return
        if server not in cache.servers:
            await evt.reply(
                "That server isn't in the previous results. If the server joined "
                "recently, you must retest the whole room."
            )
            return
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
        new_version: ServerInfo | None = None
        new_error: str | None = None

        try:
            cache.versions[server] = new_version = await asyncio.wait_for(
                self._test(server), timeout=60
            )
        except TestError as e:
            cache.errors[server] = new_error = str(e)
        except asyncio.TimeoutError:
            cache.errors[server] = new_error = "test timed out"
        except Exception:
            cache.errors[server] = new_error = "internal plugin error"

        if new_error != prev_error or new_version != prev_version:
            async with cache.lock:
                await self._edit(
                    evt.room_id, cache.event_id, self._format_results(cache), allow_html=True
                )

        if new_error is not None:
            cmd_reply_edit = f"Testing {server} failed: {new_error}"
        elif prev_version is not None:
            if prev_version == new_version:
                cmd_reply_edit = f"{server} is still on {prev_version}"
            else:
                if prev_version.software == new_version.software:
                    action = "update" if prev_version < new_version else "downgrade"
                    cmd_reply_edit = (
                        f"{server} {action}d {prev_version.software} "
                        f"from {prev_version.version} to {new_version.version}"
                    )
                else:
                    cmd_reply_edit = f"{server} switched from {prev_version} to {new_version}"
        else:
            cmd_reply_edit = f"{server} is back up and on version {new_version}"

        await self._edit(evt.room_id, event_id, cmd_reply_edit)

    @staticmethod
    def _antinotify(text: str) -> str:
        return "\ufeff".join(text)

    @classmethod
    def _user_link(cls, user_id: UserID) -> str:
        return (
            f"[{html.escape(cls._antinotify(user_id))}]"
            f"(https://matrix.to/#/{html.escape(user_id)})"
        )

    @classmethod
    def _make_user_list(cls, server_name: str, info: ServerInfo, users: list[UserID]) -> str:
        if len(users) == 1:
            user_list = cls._user_link(users[0])
        elif len(users) == 2:
            user_list = f"{cls._user_link(users[0])} and {cls._user_link(users[1])}"
        elif len(users) == 3:
            user_list = (
                f"{cls._user_link(users[0])}, {cls._user_link(users[1])} "
                f"and {cls._user_link(users[2])}"
            )
        else:
            user_list = f"{cls._user_link(users[0])}, {cls._user_link(users[1])} and {len(users) - 2} others"
        return f"* {server_name} ({info}) with {user_list}"

    @servers.subcommand(
        "upgrade",
        help="Check which servers would be left behind if the room was upgraded",
    )
    @command.argument(
        "room_version", label="stable room version", matches=r"[\d.]+", required=True
    )
    async def upgrade(self, evt: MessageEvent, room_version: str) -> None:
        if room_version not in known_room_versions:
            await evt.reply(f"Unknown room version {room_version}")
            return
        cache = await self.cached_or_test(evt)
        if not cache:
            return

        up_to_date_servers = 0
        up_to_date_users = 0
        outdated_servers = 0
        outdated_users = 0
        unknown_servers = 0
        unknown_users = 0
        outdated_matches = []
        may_contain_new_software = False
        for server_name, info in cache.versions.items():
            users = cache.servers[server_name]
            if info.software not in minimum_version:
                unknown_servers += 1
                unknown_users += len(users)
            elif info.is_new_enough(room_version):
                up_to_date_servers += 1
                up_to_date_users += len(users)
            else:
                may_contain_new_software = may_contain_new_software or info.is_unknown
                outdated_servers += 1
                outdated_users += len(users)
                outdated_matches.append(self._make_user_list(server_name, info, users))
        parts = []
        if up_to_date_servers:
            parts.append(
                f"{_pluralize(up_to_date_users, 'user')} on "
                f"{_pluralize(up_to_date_servers, 'server')} are up to date"
            )
        else:
            parts.append("Nobody is up to date 😿")
        if unknown_servers:
            are = "are" if unknown_users > 1 else "is"
            parts.append(
                f"{_pluralize(unknown_users, 'user')} on "
                f"{_pluralize(unknown_servers, 'server')} {are} using unknown software "
                f"or have faked their server's user agent"
            )
        if outdated_matches:
            outdateds = "\n".join(outdated_matches)
            are = "are" if outdated_users > 1 else "is"
            parts.append(
                "<details><summary>"
                f"{_pluralize(outdated_users, 'user')} on "
                f"{_pluralize(outdated_servers, 'servers')} {are} outdated"
                f"</summary>\n\n{outdateds}\n\n</details>"
            )
        else:
            parts.append("Nobody is outdated 🎉")
        if may_contain_new_software:
            parts.append(
                f"<sub>Room version support table last updated on {versions_updated}</sub>"
            )
        await evt.reply("\n\n".join(parts), allow_html=True)

    @servers.subcommand(
        "match",
        help=(
            "Show which servers are on a specific version. "
            "Operator can be `>`, `<`, `>=`, `<=`, `!=`, `=` or empty."
        ),
    )
    @command.argument("software", matches=".+", required=True)
    @command.argument("operator", required=False, parser=parse_operator)
    @command.argument("version", matches=".+", required=False, pass_raw=True)
    async def match(
        self,
        evt: MessageEvent,
        software: str,
        operator: ComparisonOperator | None,
        version: str,
    ) -> None:
        cache = await self.cached_or_test(evt)
        if not cache:
            return
        if not operator:
            operator = op.eq
        if not version:
            operator = lambda a, b: True
            want_info = ServerInfo(software=software, version=None)
        else:
            try:
                want_info = ServerInfo.parse(software, version)
            except ValueError as e:
                await evt.reply(str(e))
                return
        matches = []
        matched_users = 0
        matched_servers = 0
        for server_name, info in cache.versions.items():
            if info.software.lower() == want_info.software.lower() and operator(
                info.version, want_info.version
            ):
                users = cache.servers[server_name]
                matched_users += len(users)
                matched_servers += 1
                matches.append(self._make_user_list(server_name, info, users))
        if not matches:
            await evt.reply("No matches :(")
        else:
            await evt.reply(
                f"Matched {_pluralize(matched_users, 'user')} on "
                f"{_pluralize(matched_servers, 'server')}\n\n" + "\n".join(matches),
                allow_html=True,
            )
