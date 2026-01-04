# 日志配置

日志文件目前有三种配置方式，分别为配置新环境变量、配置旧环境变量和配置文件，优先级从高到底。配置旧环境变量和配置文件方式将在后续不再提供，建议使用配置新环境变量进行日志配置。新环境变量的日志配置详情请参见[MindIE日志参考](https://www.hiascend.com/document/detail/zh/mindie/23RC1/ref/logreference/mindie_log_0213.html)。

- 配置新环境变量：

    mindie组件名称取值(省略mindie前缀) [cert, ms, server, llm, sd]。

    |环境变量|默认配置|取值范围及解释|
    |--|--|--|
    |MINDIE_LOG_LEVEL|INFO|统一设置MindIE各组件日志级别。优先级高于MINDIEMS_LOG_LEVEL环境变量，如果设置环境变量，将覆盖json配置文件中log_level参数。<br>日志级别取值 [CRITICAL, ERROR, WARN, INFO, DEBUG]。|
    |MINDIE_LOG_TO_FILE|true|统一设置MindIE各组件日志是否写入文件。如果设置环境变量，将覆盖json配置文件中to_file参数。<br>取值范围为：[false, true]，且支持[0, 1]。|
    |MINDIE_LOG_TO_STDOUT|false|统一设置MindIE各组件是否打印日志。如果设置环境变量，将覆盖json配置文件中to_stdout参数。<br>取值范围为：[false, true]，且支持[0, 1]。|
    |MINDIE_LOG_VERBOSE|true|统一设置MindIE各组件日志中是否加入可选日志内容，当前日志分为固定日志内容和可选日志内容。完整调试日志格式：**[date time]** [pid] [tid] [组件名称]**[大写日志级别]** [file:line] : **[error code] [*] log message**；非加粗内容为可选内容，当环境变量设置为true时会加入可选内容。<br>取值范围为：[false, true]，且支持[0, 1]。<br>**日志格式中的[*]表示子组件或更小单位模块的名称，可以选择将其呈现在日志中，方便更好的定位问题。**|
    |MINDIE_LOG_PATH|~/mindie/log|统一设置MindIE各组件日志写入文件的保存目录。<br>若用户设置该环境变量，需要在该环境变量设置的路径后方新建log/。则调试日志记录在`$MINDIE_LOG_PATH/log/debug`路径下，安全日志默认记录在`$MINDIE_LOG_PATH/log/security`路径下。<br>- 若路径开头为"/"，则表明该路径为绝对路径；<br>- 若路径开头无"/"，则表明该路径为相对路径，且是相对于“~/mindie/log”的路径；<br>- 如果设置环境变量，将覆盖json配置文件中run_log_path和operation_log_path参数。|
    |MINDIE_LOG_ROTATE|- fs：默认值为20 (MB)<br>- r：默认值为10|统一设置MindIE各组件日志轮转。<br>设置某个组件的日志轮转格式为：*组件名称* : -fs *filesize* -r *rotate*<br>- 如果“:”前无组件名称，则默认为对所有组件统一进行设置；<br>- *filesize*表示每个日志文件大小（单位MB），取值范围 [1, 500]。如果设置环境变量，将覆盖json配置文件中max_log_file_size参数；<br>- *rotate*表示每个进程可写的最多日志文件个数，取值范围 [1, 64]。如果设置环境变量，将覆盖json配置文件中max_log_file_num参数。|


    ```
    示例1：统一将MindIE所有组件的日志级别设成debug,将集群管理组件的日志级别设置为info (用户输入的值不区分大小写)
    export MINDIE_LOG_LEVEL=debug; ms:INfo
    
    示例2：将MindIE LLM的日志级别设置成error (用户输入的值不区分大小写)
    export MINDIE_LOG_LEVEL=llm：error
    注意若输入日志级别为null，则不启用日志功能。
    
    示例3:统一将MindIE所有组件设置为打印日志(用户输入的值不区分大小写)
    export MINDIE_LOG_TO_STDOUT=1
    
    示例4:统一将MindIE所有组件的日志写入文件 (用户输入的值不区分大小写)
    export MINDIE_LOG_TO_FILE=true
    
    示例5：统一不打印或不保存MindIE所有组件的可选日志 (用户输入的值不区分大小写)
    export MINDIE_LOG_VERBOSE=false
    
    示例6:打印或保存集群管理组件的可选日志(用户输入的值不区分大小写)
    export MINDIE_LOG_VERBOSE=ms:true”
    
    示例7：设置集群管理组件日志写入文件的保存目录为/home/dev
    export MINDIE_LOG_TO_FILE=ms:/home/dev
    
    示例8：设置集群管理组件的轮转：每个文件大小上限为10MB, 每个进程文件数上限为20
    export MINDIE_LOG_ROTATE=ms:-fs 10 -r 20
    ```

    >[!NOTE]说明
    >同一条日志配置命令中，后方配置覆盖前方。相当于将日志配置使用分号”;”分割，逐条导入。
    >- 例1：export MINDIE_LOG_LEVEL="llm: info; llm: warn"，其等价于export MINDIE_LOG_LEVEL="llm: info"; export MINDIE_LOG_LEVEL="llm: warn"，将llm的日志级别设置为warn，后一个配置覆盖前一项配置。
    >- 例2：export MINDIE_LOG_LEVEL="llm: info; warn"，其等价于export MINDIE_LOG_LEVEL="llm: info"; export MINDIE_LOG_LEVEL="warn"则表示llm的日志级别为warn，因为后面一项warn未指定特定组件，则对所有组件生效，覆盖对llm的设置。

- 配置旧环境变量：

    |环境变量|取值范围及解释|
    |--|--|
    |MINDIEMS_LOG_LEVEL|**当前保留MINDIEMS_LOG_LEVEL是为了兼容旧版本配置方式。**<br>**若MINDIE_LOG_LEVEL设置为空则使用MINDIEMS_LOG_LEVEL。**<br>用户可动态设置集群管理组件客户端输出的日志等级。<br>默认值为空，环境变量的优先级高于json配置文件中log_level参数。日志级别如下所示：<br>- DEBUG<br>- INFO<br>- WARNING<br>- ERROR<br>- CRITICAL|


    集群管理组件客户端可通过MINDIEMS\_LOG\_LEVEL环境变量动态设置日志打印等级，如下所示：

    ```bash
    export MINDIEMS_LOG_LEVEL={日志打印等级}
    ```

    - DEBUG
    - INFO
    - WARNING
    - ERROR
    - CRITICAL

- 配置文件：
    - 客户端日志会根据 *{$HOME}*/.mindie_ms/msxxx.json配置的日志等级log_level参数进行过滤，将日志内容打印到客户端屏幕上。
    - 服务端日志会根据服务端配置文件ms_xxx.json中的以下代码进行设置。

        ```
        "log_info": {
            "log_level": "INFO",                             // 日志级别
            "run_log_path": "/var/log/mindie-ms/run/log.txt",  // 运行日志写入的文件路径
            "operation_log_path": "/var/log/mindie-ms/operation/log.txt" //操作日志写入的文件路径
            "max_log_file_size": 20,  // 最大日志文件大小
            "max_log_file_num": 10 //最大日志文件数量
        }
        ```

        >[!NOTE]说明
        >- 过滤等级后，将日志内容写入"log_path"路径中的日志文件，服务端报错可以通过日志进行定位；当前日志写入策略是循环写入，默认最多保存10个日志文件，默认每个日志文件最大为20M。
        >-   将日志写入日志文件时，需要导入以下KMC依赖的环境变量。
        >       ```bash
        >       export HSECEASY_PATH=$MIES_INSTALL_PATH/lib
        >       ```
        >-  对于用户将配置文件中的必选项配置错误，导致的服务启动失败的情况，将通过打印并落盘保存错误日志信息到默认目录~/mindie/log来提示以便问题定位。

