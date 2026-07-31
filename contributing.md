# 贡献者指南

## Dev Container 开发环境

仓库提供了 `.devcontainer` 配置。安装 Docker 和 VS Code
[Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
扩展后，在仓库目录中选择 **Dev Containers: Reopen in Container**，即可创建 Python 3.11
开发环境。容器创建完成后会自动安装 `requirements.txt` 中的依赖，并以 editable 模式安装
MindIE-Motor。

该开发容器适用于代码开发、静态检查和不依赖昇腾硬件的单元测试。需要 NPU、CANN 或其他
昇腾运行时的集成测试仍需在相应硬件环境中执行。

## 贡献流程与规范

- [issue提交指南](https://gitcode.com/Ascend/community/blob/master/docs/contributor/issue-guide.md)
- [社区 Issue 处理流程指导](https://gitcode.com/Ascend/community/blob/master/docs/contributor/issue-workflow-guidelines.md)
- [PR提交指南](https://gitcode.com/Ascend/community/blob/master/docs/contributor/pr-guide.md)
- [Ascend 社区开发者测试贡献指南](https://gitcode.com/Ascend/community/blob/master/docs/contributor/developer-testing-guide.md)
- [Ascend 开源与第三方软件建仓及分支命名指导](https://gitcode.com/Ascend/community/blob/master/docs/contributor/third-party-repo-branch-guide.md)
- [Ascend 开源与第三方软件管理规范](https://gitcode.com/Ascend/community/blob/master/docs/contributor/third-party-software-management-guide.md)
- [社区安全设计规范](https://gitcode.com/Ascend/community/blob/master/docs/contributor/security-design-guideline.md)
