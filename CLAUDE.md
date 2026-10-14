# Working on agent-tool-selfupdate

- Python 3.11+, standard library only. Do not add dependencies.
- Run the tests with `python3 -m unittest discover -s tests`. Every change needs a test. Tests use the fakes in `tests/helpers.py` and must not need the network, not even loopback, nor the real home: an installation runs them in a bubblewrap sandbox with home `/home/sandbox`.
- This library is what lets the services repair themselves. A change that breaks finding, checking or installing a release locks every service out of its next update. Keep the probe (`check`) working and keep `update` backward compatible inside a major version.
- Version numbers are a promise to the projects (`~=1.0` in their selfupdate.toml): patch for fixes, minor for additions, major for anything a project's adapter must change for. Raise `__version__` in `agent_tool_selfupdate/version.py` in the same pull request. The owner publishes the release tag by hand.
- The trust model is "a release published by the configured login". Do not add other ways for code to arrive (no pip, no branches, no URLs from a release body).
- Archives are unpacked only through `archive.unpack`, which refuses links, `..` and absolute paths.
- Never log or return tokens. A `GITHUB_TOKEN` from the environment is only sent to api.github.com.
- Code, comments, commit messages and pull requests are in English.
