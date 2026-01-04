# 环境变量

集群管理组件的环境变量如下所示：

|参数名称|参数说明|取值范围|缺省值|
|--|--|--|--|
|MINDIEMS_LOG_LEVEL|用户可动态设置集群管理组件客户端输出的日志等级。|<li>DEBUG<li>INFO<li>WARN<li>ERROR<li>CRITICAL|默认值为空，设置为表3中log_level参数的日志等级。|
|HOME|用户动态设置集群管理组件客户端msctl.json配置文件的路径。|存在可读取的*{$HOME}*/.mindie_ms/msctl.json文件，详情请参考表1。|<li>root用户：默认值为/root。<li>非root用户：默认值为/*{$HOME}*/*{非root用户名}*。|
|MINDIE_MS_SERVER_IP|集群管理组件服务端容器化部署时容器的Pod IP地址。|取值必须为部署的容器IP，需与3.a样例中"- name: MINDIE_MS_SERVER_IP"部分的格式保持一致。|默认为集群管理组件服务端容器化部署时容器Pod IP的地址。|


