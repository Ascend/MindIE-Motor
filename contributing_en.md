# Contribution Guide

## Dev Container Development Environment

The repository provides a `.devcontainer` configuration. After installing Docker and the VS Code [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension, select **Dev Containers: Reopen in Container** in the repository directory to create a Python 3.11 development environment. After the container is created, the dependencies in `requirements.txt` are automatically installed, and MindIE Motor is installed in editable mode.

This development container is suitable for code development, static checks, and unit tests that do not depend on Ascend hardware. Integration tests that require NPUs, CANN, or other Ascend runtimes must still be executed in the corresponding hardware environment.

## Contribution Process and Standards

- [Issue Submission Guide](https://gitcode.com/Ascend/community/blob/master/docs/contributor/issue-guide.md)
- [Community Issue Workflow Guidelines](https://gitcode.com/Ascend/community/blob/master/docs/contributor/issue-workflow-guidelines.md)
- [PR Submission Guide](https://gitcode.com/Ascend/community/blob/master/docs/contributor/pr-guide.md)
- [Ascend Community Developer Testing Contribution Guide](https://gitcode.com/Ascend/community/blob/master/docs/contributor/developer-testing-guide.md)
- [Ascend Open-Source and Third-Party Software Repository Creation and Branch Naming Guide](https://gitcode.com/Ascend/community/blob/master/docs/contributor/third-party-repo-branch-guide.md)
- [Ascend Open-Source and Third-Party Software Management Standards](https://gitcode.com/Ascend/community/blob/master/docs/contributor/third-party-software-management-guide.md)
- [Community Security Design Guidelines](https://gitcode.com/Ascend/community/blob/master/docs/contributor/security-design-guideline.md)
