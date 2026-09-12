# Count log lines by level

`input.log` in the working directory is a small application log. Each line has
the form:

```
<timestamp> <LEVEL> <message>
```

Write a file named `out.json` in the working directory. It must be a JSON
object that maps each log level name to the number of lines carrying that
level. For example, if the log contained three `INFO` lines and one `ERROR`
line, the object would be `{"INFO": 3, "ERROR": 1}`.

Use the level names exactly as they appear in the log. Only plain `python3`
(or another tool already on the host) is available: no network and no package
installs.
