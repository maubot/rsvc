# rsvc
A [maubot](https://github.com/maubot/maubot) to check the version of servers in
rooms.

## Commands
- `!servers` - Test all servers in room and report aggregated tests
- `!servers retest <name>` - Re-test a server and update the previous results
- `!servers match <software> [operator] <version>` - Show all servers with the
  given software and version. Operator can be `>`, `<`, `>=`, `<=`, `!=`, `=`
  or empty
- `!server`, `!version` and `!versions` are aliases for `!servers`, `recheck`
  is an alias for `tetest`
