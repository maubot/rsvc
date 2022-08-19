# rsvc
A [maubot](https://github.com/maubot/maubot) to check the version of servers in
rooms.

## Commands
- `!servers` - Test all servers in room and report aggregated tests.
- `!servers retest <name>` - Re-test a server and update the previous results.
- `!servers match <software> [operator] <version>` - Show all servers with the
  given software and version. Operator can be `>`, `<`, `>=`, `<=`, `!=`, `=`
  or empty.
- `!servers upgrade <stable room version>` - Check which servers would be left
  behind if the room was upgraded.
- `!server test <name>` - Test one server, independently of any previous whole-room tests.
- `!server`, `!version` and `!versions` are aliases for `!servers`, `recheck`
  is an alias for `tetest`.
