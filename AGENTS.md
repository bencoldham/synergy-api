# AGENTS.md
- This project is being developed in a Bluefin Dev Container; install any missing dependencies as needed.
- Never wrap an exception with an abstraction without the original exception being chained and visible in logs.
- Do NOT write overly verbose code, do not handle every single exception, just let it explictly fail.
- Do NOT write tests beyond live_test.py.w