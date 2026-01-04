# 说明


- [集群内通信接口](./intra_cluster_communication_interface.md)的使用权限应为管理员，且该部分接口只能在集群内访问。
- 配置ulimit值。
    用户需要注意运行环境的最大文件数，使用以下命令查看环境中ulimit值的上限：
    ```bash
    ulimit -n
    ```
    
    Coordinator由于使用HTTPS与用户通信，在收到推理请求时，会生成socket系统文件。socket系统文件的数量与并发推理请求数相关，当超过最大文件数上限时，会导致Coordinator程序运行失败。
    如果最大文件数太低，建议配置ulimit值，配置为：3\*最大并发请求数。使用以下命令配置ulimit值：
    例如：最大并发请求数为500，其值建议配置为3\*500=1500。
    ```bash
    ulimit -n 1500
    ```

