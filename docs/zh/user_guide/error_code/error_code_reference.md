# MindIE Motor错误码参考

## 错误码展示方式说明

MindIE Motor组件后6位错误码，16进制。以100201为例，各段含义如[表1](#table172676258115)所示。

**表 1**<a name='table172676258115'></a> 错误码各段说明

|错误码字段|说明|
|--|--|
|10（前2位）|子组件编码。子组件编码参考：<br/><ul><li>10：ms-controller</li><li>20：ms-coordinator</li><li>30：ms-deployer</li><li>40：ms-common（包含httpServer等）</li></ul>|
|02（中间2位）|特性编码。|
|01（最后2位）|错误类型。编码参考：<br/><ul><li>00：EXCEPTION，异常捕获，具体错误参考异常打印信息。</li><li>01：INVALID_INPUT，外部输入的参数不合法。</li><li>02：INVALID_PARAMETER，参数校验出错，非外部输入。</li><li>03：OUT_OF_RANGE，超出范围，尝试的操作超出了有效范围。</li><li>04：OPERATION_REPEAT，重复操作。尝试创建/插入的实体（例如：文件或目录）已存在，或尝试操作（设置状态等）的实体已被操作过。</li><li>05：NOT_FOUND，未找到某些实体（例如：文件、目录、请求或方法等）。</li><li>06：RESOURCE_LIMIT，资源值达到设定的阈值（最大或最小）。</li><li>07：RESOURCE_EXHAUSTED，资源已经用尽。物理资源；CPU、磁盘空间和内存空间过载等。例如：内存申请失败。</li><li>08：DEADLINE_EXCEEDED，截止时间已过期。操作超时，网络链路延迟（time_out）。</li><li>09：UNREACHABLE，通信链路中断。</li><li>0A：CALL_ERROR，执行函数内部逻辑失败引起的外部报错，报错原因是多样的，需要参考执行函数内的报错信息。</li><li>0B：UNAVAILABLE，服务或资源不支持或不可用（状态上或功能上）。</li><li>0C：FAILED_PRECONDITION，当前系统状态拒绝操作。例如：要删除的目录非空等。</li><li>0D：PENDING，操作中断，可能是由于并发问题或是异常退出。</li><li>0E：DATA_LOSS，无法恢复的数据丢失或损坏。</li><li>0F：UNAUTHENTICATED，该请求没有有效身份验证凭证。</li><li>10：PERMISSION_DENIED，无操作权限。</li></ul>|

## MIE03E400008

**故障现象**

HTTPServer通信超时，建立连接失败、请求执行失败。

**故障原因**

等待时间超过设定的时延。

**故障定位方法**

检查请求的服务是否已经拉起。

**推荐调试方式**

尝试重新执行请求，或重启服务。

## MIE03E400002

**故障现象**

HTTPServer参数校验出错，对应子组件的HTTPServer启动失败。

**故障原因**

设置上下文校验模式时出现错误。

**故障定位方法**

查看故障日志中failed后面的具体哪些参数校验失败，根据参数尝试定位问题。

## MIE03E400009

**故障现象**

HTTPServer通信失败，建立连接失败、请求执行失败。

**故障原因**

通信链路中断。

**故障定位方法**

查看建链失败的Boost状态码，根据状态码定位失败原因。

## MIE03E400006

**故障现象**

HTTPServer资源受限，建立连接失败。

**故障原因**

HTTPServer连接数达到设定的上限。

**故障定位方法**

检查连接数量，是否重复连接。

## MIE03E400007

**故障现象**

HttpServer初始化申请内存失败，对应子组件的HttpServer启动失败。

**故障原因**

HttpServer初始化时申请内存失败，一般是创建会话或侦听器失败。

**故障定位方法**

检查内存资源是否耗尽。

## MIE03E400005

**故障现象**

HttpServer配置文件不存在，对应子组件的HttpServer启动失败。

**故障原因**

HttpServer内证书/密钥文件缺失。

**故障定位方法**

检查证书路径是否配置正确。

## MIE03E400001

**故障现象**

HTTPServer配置项不合法，对应子组件的HTTPServer启动失败，子组件启动失败。

**故障原因**

HTTPServer内添加密钥不合法。

**故障定位方法**

检查证书文件是否正确。

## MIE03E100005,MIE03E100305,MIE03E100405,MIE03E100505,MIE03E100705,MIE03E100805,MIE03E100905,MIE03E100B05,MIE03E100D05,MIE03E100F05

**故障现象**

Controller查询不到请求的资源，请求执行失败。

**故障原因**

资源（节点、请求等）未查询到。

**故障定位方法**

查看日志中报错的请求参数。

## MIE03E100007,MIE03E100207,MIE03E100307,MIE03E100407,MIE03E100707,MIE03E100907,MIE03E100C07,MIE03E100D07,MIE03E101007

**故障现象**

Controller申请内存失败，Controller启动失败。

**故障原因**

Controller组件内出现申请内存失败，可能是系统资源不足导致的创建对象或是线程失败。

**故障定位方法**

检查内存资源是否耗尽。

## MIE03E10000A,MIE03E10010A,MIE03E10060A,MIE03E10070A,MIE03E10090A,MIE03E100B0A,MIE03E100C0A,MIE03E100F0A,MIE03E10100A

**故障现象**

Controller内部逻辑执行失败，业务执行失败。

**故障原因**

内部逻辑失败引发的故障日志，具体故障原因参考附近的下层日志。

**故障定位方法**

请提交ISSUE。

## MIE03E100404,MIE03E100C04

**故障现象**

Controller资源重复操作，请求执行失败。

**故障原因**

节点被重复注册或URL handler被重复注册。

**故障定位方法**

参考日志中报错的请求参数，或提交ISSUE。

## MIE03E100501,MIE03E100601,MIE03E100701,MIE03E100801,MIE03E100901,MIE03E100A01,MIE03E100C01,MIE03E100D01

**故障现象**

Controller输入配置不合法，Controller组件启动失败。

**故障原因**

输入的配置文件中某些配置项不存在或不合法。

**故障定位方法**

检查日志中提示报错的配置项。

## MIE03E100500,MIE03E100700,MIE03E100900,MIE03E100A00,MIE03E100B00,MIE03E100D00,MIE03E100F00

**故障现象**

Controller捕获异常报错，业务执行失败。

**故障原因**

一般是处理JSON和string转换时捕获的异常。

**故障定位方法**

查看抛出的异常信息，提交ISSUE。

## MIE03E100702,MIE03E100802,MIE03E100B02,MIE03E100F02

**故障现象**

Controller参数校验出错，请求执行失败。

**故障原因**

返回的response校验失败或是一些空值异常。

**故障定位方法**

查看报错日志中的response信息或校验信息。

## MIE03E10070B,MIE03E10090B,MIE03E100F0B

**故障现象**

Controller当前资源不可用，请求执行失败。

**故障原因**

节点状态不可用或方法/硬件类型不支持。

**故障定位方法**

查看打印的资源ID、IP或类型等信息。

## MIE03E100706,MIE03E100906,MIE03E100D06

**故障现象**

Controller资源受限，请求执行失败。

**故障原因**

节点数量或分组数量超过了设定的上限。

**故障定位方法**

尝试查询当前Group和Node信息，或提交ISSUE。

## MIE03E100709,MIE03E100B09,MIE03E100E09,MIE03E100F09

**故障现象**

Controller通信失败，请求执行失败。

**故障原因**

Controller组件内发送HTTP请求失败，可能是连接中断或请求体不合法。

**故障定位方法**

尝试重新执行请求或重启服务。

## MIE03E20000A,MIE03E20010A,MIE03E20030A,MIE03E20050A,MIE03E20060A,MIE03E200B0A,MIE03E200C0A,MIE03E20170A,MIE03E20190A

**故障现象**

Coordinator内部逻辑失败，业务执行失败。

**故障原因**

内部逻辑失败引发的故障日志，具体故障原因参考下层日志。

**故障定位方法**

请提交ISSUE。

## MIE03E200101,MIE03E200501,MIE03E200601,MIE03E200901,MIE03E200A01,MIE03E200C01,MIE03E201301,MIE03E201701,MIE03E201801

**故障现象**

Coordinator输入配置不合法，Coordinator启动失败。

**故障原因**

Coordinator输入的配置文件中某些配置项不存在或不合法。

**故障定位方法**

检查日志中报错的配置项，按照[user_config.json配置文件全量参数说明](../configuration/config_reference.md#motor_coordinator_config)要求修改报错的user\_config.json配置项。

## MIE03E200100,MIE03E200600,MIE03E200700,MIE03E200A00,MIE03E200B00,MIE03E200C00,MIE03E201400

**故障现象**

Coordinator捕获异常报错，业务执行失败。

**故障原因**

通常是初始化处理JSON时的异常、集群实例异常以及请求异常。

**故障定位方法**

根据抛出的异常信息尝试定位，或提交ISSUE。

## MIE03E200204,MIE03E200C04,MIE03E201404

**故障现象**

Coordinator资源重复操作，请求执行失败。

**故障原因**

集群管理重复添加实例ID/请求ID。

**故障定位方法**

内部资源请求重复执行，提交ISSUE。

## MIE03E200305,MIE03E200505,MIE03E200605,MIE03E200A05,MIE03E200C05,MIE03E200D05,MIE03E200F05,MIE03E201005,MIE03E201105,MIE03E201205,MIE03E201305,MIE03E201405,MIE03E201505,MIE03E201605,MIE03E201905

**故障现象**

Coordinator查询不到请求的资源，请求执行失败。

**故障原因**

节点/连接/请求等资源未查询到。

**故障定位方法**

请提交ISSUE。

## MIE03E200409,MIE03E200509,MIE03E200609,MIE03E200709,MIE03E200809,MIE03E200B09,MIE03E200C09,MIE03E200E09,MIE03E200F09,MIE03E201409,MIE03E201509,MIE03E201609

**故障现象**

Coordinator通信失败，请求执行失败。

**故障原因**

HTTP请求体不合法或通信失败。

**故障定位方法**

尝试重新执行请求或重启服务。

## MIE03E200506,MIE03E200F06

**故障现象**

Coordinator资源受限，请求执行失败。

**故障原因**

HttpClientAsync连接上限或是请求重试次数上限。

**故障定位方法**

尝试重新执行请求或重启服务。

## MIE03E200502,MIE03E200602,MIE03E201502,MIE03E201902

**故障现象**

Coordinator参数校验出错，业务执行失败。

**故障原因**

初始时设置TLS上下文失败/从Server读取计算token的结果失败。

**故障定位方法**

请提交ISSUE。

## MIE03E20070D

**故障现象**

Coordinator操作中断，请求执行失败。

**故障原因**

异步处理还未结束，DoWriteFinishRes和DoWriteChunk同时到来。

**故障定位方法**

尝试重新执行请求或重启服务。

## MIE03E200A0B,MIE03E200D0B,MIE03E200F0B,MIE03E20110B,MIE03E20120B,MIE03E20130B,MIE03E20190B,MIE03E201A0B

**故障现象**

Coordinator当前配置/资源不可用，请求执行失败。

**故障原因**

当前配置不可用/节点连接失败/当前节点不可用/无可用节点。

**故障定位方法**

请提交ISSUE。

## MIE03E201008

**故障现象**

Coordinator通信超时，业务执行失败。

**故障原因**

请求调度/推理/首token/tokenizer超时。

**故障定位方法**

尝试重新执行请求或重启服务。

## MIE03E300001,MIE03E300101,MIE03E300301,MIE03E300401,MIE03E300501,MIE03E300601,MIE03E300701

**故障现象**

Deployer输入配置不合法，Deployer启动失败。

**故障原因**

Deployer输入的配置文件中某些配置项不存在或不合法。

**故障定位方法**

根据日志报错检查user\_config.json中的配置项及所对应的配置文件。

## MIE03E30000B,MIE03E30040B

**故障现象**

Deployer当前类型不支持，Deployer执行失败。

**故障原因**

当前不支持的硬件类型/部署参数设置。

**故障定位方法**

按照[user_config.json配置文件全量参数说明](../configuration/config_reference.md)要求修改报错的user\_config.json/env.json配置项。

## MIE03E300006,MIE03E300306,MIE03E300406

**故障现象**

Deployer资源受限，Deployer请求执行失败。

**故障原因**

实例数量超过数量上限。

**故障定位方法**

检查传入的user\_config.json的配置是否正确，并进行修改。

## MIE03E300009

**故障现象**

Deployer通信失败，Deployer请求执行失败。

**故障原因**

发送k8s请求失败。

**故障定位方法**

检查传入的user\_config.json配置，尝试重新执行请求或重启服务。

## MIE03E30000A,MIE03E30030A,MIE03E30040A,MIE03E30060A,MIE03E30070A

**故障现象**

Deployer内部逻辑失败，Deployer业务执行失败。

**故障原因**

内部逻辑失败引发的故障日志，具体故障原因参考附近下层日志。

**故障定位方法**

请提交ISSUE。

## MIE03E300002

**故障现象**

Deployer参数校验出错，Deployer业务执行失败。

**故障原因**

k8s响应参数校验失败。

**故障定位方法**

请提交ISSUE。

## MIE03E300005,MIE03E300205,MIE03E300305,MIE03E300505,MIE03E300605,MIE03E300705

**故障现象**

Deployer所需配置文件不存在，Deployer启动失败。

**故障原因**

Deployer启动执行时配置文件缺失。

**故障定位方法**

按照[deploy.py使用方法](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/README.md#deploypy使用方法)和[配置文件说明](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/README.md#配置文件说明)检查是否正确指定配置文件路径。

## MIE03E300007

**故障现象**

Deployer申请资源失败，Deployer启动失败。

**故障原因**

创建线程实例时失败。

**故障定位方法**

检查内存资源是否耗尽。

## MIE03E300204,MIE03E300304,MIE03E300404

**故障现象**

Deployer资源重复操作，Deployer请求执行失败。

**故障原因**

推理服务创建器/推理服务被重复插入/创建。

**故障定位方法**

内部资源重复注册，提交ISSUE。

## MIE03E300400

**故障现象**

Deployer捕获异常报错，Deployer业务执行失败。

**故障原因**

初始化配置时字符串处理异常。

**故障定位方法**

查看异常原因，修改配置文件。
