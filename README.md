# memorysafe-ci

A CI mirror of a private repository. Everything here is generated: each `candidate/*` and `release-candidate/*` branch is a single commit, pushed by a script and overwritten by the next push, so that the checks can run on public runners.

This repository takes no pull requests and no issues. MemorySafe itself is installed from [MemorySafe-Labs/memorysafe-plugin](https://github.com/MemorySafe-Labs/memorysafe-plugin).
