# 部署Service服务时出现LLMPythonModel initializes fail的报错提示

**问题描述**

部署Service服务时，出现LLMPythonModel initializes fail的报错，如下图所示。

![](../figures/faq001.png)

**原因分析**

ibis缺少Python依赖。

**解决步骤**

进入/_Service安装路径_/logs目录，打开Python日志，根据日志报错信息，安装需要的依赖。

# 加载模型时出现out of memory报错提示

**问题描述**

部署Service服务，加载模型时出现out of memory报错提示，如下图所示。

![](../figures/faq002.png)

**原因分析**

权重太大，内存不足。

**解决步骤**

将config.json文件中ModelConfig的npuMemSize调小，比如调成8。

# 部署Service服务时，出现atb\_llm.runner无法import报错

**问题描述**

部署Service服务时，出现atb\_llm.runner无法import，如下图所示。

![](../figures/faq003.png)

**原因分析**

由于Python版本不是配套版本3.10，或者pip对应的Python版本不是目标版本3.10，找不到对应的包。可以通过python和pip -v查看对应的Python版本进行确认。

**解决步骤**

1.  使用以下命令打开bashrc文件。

    ```
    vim ~/.bashrc
    ```

2.  在bashrc文件内添加如下环境变量，保存并退出。

    ```
    ## 例如系统使用3.10.13版本，安装目录位于/usr/local/python3.10.13
    export LD_LIBRARY_PATH=/usr/local/python3.10.13/lib:$LD_LIBRARY_PATH
    export PATH=/usr/local/python3.10.13/bin:$PATH
    ```

3.  使用以下命令使环境变量生效。

    ```
    source ~/.bashrc
    ```

4.  使用以下命令建立软链接。

    ```
    ln -s /usr/local/python3.10.13/bin/python3.10 /usr/bin/python
    ln -s /usr/local/python3.10.13/bin/pip3.10 /usr/bin/pip
    ```

# 部署Service服务时，找不到tlsCert等路径

**问题描述**

部署Service服务时，找不到tlsCert等路径，如下图所示。

![](../figures/faq004.png)

**原因分析**

开启HTTPS服务时，未将需要的证书放到对应的目录下。

**解决步骤**

将生成服务器证书、CA证书、和服务器私钥等认证需要的文件，放置在对应的目录。

# 使用集群管理组件部署单机任务后系统出现异常，不能通过集群管理组件客户端卸载

**问题描述**

使用集群管理组件部署单机任务后系统出现异常，不能通过集群管理组件客户端卸载，需要手动执行kubectl命令删除相关资源。

**解决步骤**

执行kubectl命令手动删除相关资源，命令如下：

```
kubectl delete deployment {server_name} -n {namespace}
kubectl delete service {server_name} -n {namespace}
```

_\{server\_name\}_：是用户服务配置中[表2](准备Deployer-msctl命令行客户端.md#table67213616914)的server\_name。

_\{namespace\}_：是用户服务配置中[表2](K8s集群部署Deployer服务端.md#table67213616398)的namespace。

# 使用集群管理组件部署多机任务后系统出现异常，不能通过集群管理组件客户端卸载

**问题描述**

使用集群管理组件部署多机任务后系统出现异常，不能通过集群管理组件客户端卸载，需要手动执行kubectl命令删除相关资源。

**解决步骤**

执行kubectl命令手动删除相关资源，命令如下：

```
kubectl delete deployment {server_name}-deployment-0 -n {namespace}
kubectl delete cm rings-config-{server_name}-deployment-0 -n {namespace}
kubectl delete service {server_name}-service -n {namespace}
```

_\{server\_name\}_：是用户服务配置中[表2](准备Deployer-msctl命令行客户端.md#table67213616914)的server\_name。

_\{namespace\}_：是用户服务配置中[表2](K8s集群部署Deployer服务端.md#table67213616589)的namespace。

# 使用第三方库transformers跑模型推理时，报错“cannot allocate memory in static TLS block”

**问题描述**

报错详细信息如下所示：

![](../figures/faq005.png)

**原因分析**

glibc.so本身的bug。

**解决步骤**

执行以下命令：

```
export LD_PRELOAD=$LD_PRELOAD:/usr/local/python3.10.2/lib/python3.10/site-packages/torch/lib/../../torch.libs/libgomp-6e1a1d1b.so.1.0.0
```
# 加载大模型时耗时过长<a id="jzdmxshsgc"></a>

**问题描述**

加载1300B大小的模型时耗时过长（约3个小时）。其中"B"代表"Billion"，即十亿。

**原因分析**

未使用异步加载。

**解决方法**

通过设置环境变量OMP\_NUM\_THREADS进行模型加载优化，OMP\_NUM\_THREADS用于设置OpenMP（Open Multi-Processing）并行编程框架的线程数量，设置后加载1300B大小的模型只要10分钟左右。

```
export OMP_NUM_THREADS=1
```

此外，使用下面命令启动NPU显存碎片收集。

```
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export NPU_MEMORY_FRACTION=0.96
```

# 多模态模型输入时，出现大小限制报错问题

**问题描述**

多模态模型输入（image\_url/video\_url/audio\_url）时出现类似以下报错提示：

-   OpenAI接口：

    ```
    {"error": "Message len not in (0, 4194304], but the length of inputs is xxxxx", "error_type": "Input Validation Error"}
    ```

-   vLLM接口：

    ```
    Prompt must be necessary and data type must be string and length in (0, 4194304], but the length of inputs is xxxxx
    ```

-   Triton接口：

    ```
    Text_input must be necessary and data type must be string and length in (0, 4194304], but the length of inputs is xxxxx
    ```

<br>

**原因分析**

可能输入的图片、音频或者视频是base64编码格式（Base64 编码后的数据通常是原始数据的4/3倍），导致整个message/prompt/text\_input超过4MB而出现报错。


<br>

**解决方案**

-   方式一：参考接口约束

    -   OpenAI接口：

        请求参数中的messages参数下所有字段的字符数被限制为不大于4MB，详情请参见[推理接口]。

    -   vLLM接口：

        请求参数中的prompt参数下所有字段的字符数被限制为不大于4MB，详情请参见《MindIE LLM开发指南》中的“API接口说明 \> RESTful API参考 \> EndPoint业务面RESTful接口 \> 兼容vLLM 0.6.4版本接口 \> 文本/流式推理接口”。

    -   Triton接口：

        请求参数中的text\_input参数下所有字段的字符数被限制为不大于4MB，详情请参见《MindIE LLM开发指南》中的“API接口说明 \> RESTful API参考 \> EndPoint业务面RESTful接口 \> 兼容Triton接口 \> 文本推理接口”。

    >![](public_sys-resources/icon-note.gif) **说明：** 
    >-   假如当base64编码的图片格式大小为1MB，message/prompt/text\_input下其余的请求字符数大于3MB时，也会导致整个message/prompt/text\_input超过4MB而出现报错。
    >-   如果image\_url/video\_url/audio\_url传的是本地图片/视频/音频，或者是一个远端的url，这个url的字符串长度大小加上message/prompt/text\_input下其余的请求字符数大小也应该小于4MB。url传进来之后，会去加载解析该url得到该图片/视频/音频：
    >    -   图片：不大于20MB；
    >    -   视频：不大于512MB；
    >    -   音频：不大于20MB；
    >-   base64编码的输入更容易超出限制，当前版本会报错， 因为涉及到安全问题，建议采用网页地址或本地地址。
    >-   如果选择使用base64格式请求，请勿直接使用终端curl，建议选择使用Python脚本，因为base64编码后的数据字符长度可能超出系统终端限制，导致请求被截断。

-   方式二：手动修改源码

    比如修改输入inputs大小的限制为10MB，代码修改示例如下所示：

    **图 1**  示例一

    ![](../figures/faq006.png)

    **图 2**  示例二

    ![](../figures/faq007.png)


# 多模态模型推理服务时报错：RuntimeError: call calnnCat failed, detail:EZ1001

**问题描述**

多模态模型推理服务时，文件MindIE-LLM-master\\examples\\atb\_models\\atb\_llm\\models\\qwen2\_vl\\flash\_causal\_qwen2\_using\_mrope.py出现类似以下报错提示：

```
call calnnCat failed, detail:EZ1001: xxxxxxxx dimnum of tensor 5 is [1], should be equal to tensor 0 [2].
```

**图 1**  报错信息

![](../figures/faq008.png)

**图 2**  报错文件

![](../figures/faq009.png)

**图 3**  报错文件

![](../figures/faq010.png)

<br>

**原因分析**

可能是concat相关的某个算子，tensor 5在某个维度上是1，与要求的维度上是2大小不一致。可能与squeeze有关，因为squeeze会去掉大小为1的维度。

示例：如果某个算子（如：concat、matmul等），希望这个维度存在并匹配某个值（如：2），那被squeeze删除后shape就会报错。

>![](public_sys-resources/icon-note.gif) **说明：** 
>MindIE 2.0之前的版本存该问题，MindIE 2.0版本之后都已修复。

<br>

**解决方案**

修改代码，如下所示：

![](../figures/faq013.png)

# 运行Qwen2.5-VL系列模型失败并报错

**问题描述**

运行Qwen2.5-VL系列模型失败并出现类似以下报错提示：

-   报错提示一：

    ```
    You are using a model of type qwen2_5_vl to instantiate a model of type. This is not supported for all configurations of models and can yiled errors.
    ```

-   报错提示二：

    ```
    [ERROR] TBE Subprocess[task_distribute] raise error[], main process disappeared!
    ```

<br>

**原因分析**

模型配置等不支持，一般是因为安装的依赖不正确，需要安装对应的依赖文件。

<br>

**解决方案**

-   报错提示一处理方式：

    根据每个模型所需依赖安装对应的requirements.txt 文件。

    -   所有模型需要安装的通用依赖文件所在路径为：

        ```
        /usr/local/Ascend/atb-models/requirements/requirements.txt
        ```

    -   不同的模型对应的依赖安装文件在models路径下，比如qwen2-vl 模型：

        ```
        /usr/local/Ascend/atb-models/requirements/models/requirements_qwen2_vl.txt
        ```

    安装命令如下所示：

    ```
    pip install -r /usr/local/Ascend/atb-models/requirements/models/requirements_qwen2_vl.txt
    ```

-   报错提示二处理方式：

    1.  单击[链接](https://modelers.cn/models/MindIE/DeepSeek-R1-Distill-Llama-70B)查看该模型硬件环境是否支持。
    2.  使用以下命令排查驱动版本是否配套，驱动最低版本23.0.7才能运行，建议安装驱动版本为24.1.RC2及以上。

        ```
        npu-smi info
        ```

    3.  初始环境变量检查下是否都已经配置好，并且已经生效。
    4.  检查系统空闲内存是否充足。

        使用以下命令查看free的内存大小，需保证大于**权重大小/机器数**。

        ```
        free -h
        ```

        根据经验，尽量保证**free\_mem \>= \(权重大小/机器数\) \* 1.3**。

        >![](public_sys-resources/icon-note.gif) **说明：** 
        >每次跑完模型，请检查一下机器的host侧内存占用，避免内存不足导致模型运行失败。

    5.  导入以下环境变量：

        ```
        export HCCL_DETERMINISTIC=false
        export HCCL_OP_EXPANSION_MODE="AIV"
        export NPU_MEMORY_FRACTION=0.96
        export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
        ```

    6.  排查多机服务化参数配置是否一致。
    7.  重启服务器，并重新启动服务。

    >![](public_sys-resources/icon-note.gif) **说明：** 
    >-   硬件环境、版本配套，驱动、镜像等版本更新到最新版能有效避免很多类似此类报错问题。
    >-   该报错更多处理方式请参见[链接](https://www.hiascend.com/developer/blog/details/02112175404775067102)。

# 纯模型推理和服务化拉起或推理时，出现Out of memory（OOM）报错

**问题描述**

纯模型推理和服务化拉起/推理时，出现各种Out of memory（OOM）报错，报错信息类似如下所示：

```
RuntimeError: NPU out of memory. Tried to allocate xxx GiB."
```
<br>

**原因分析**

-   模型权重文件较大。
-   用户输入shape超大（batch size较大、过长的文本、过大的图片、音频或视频）。
-   配置文件参数配置超大。

<br>

**解决方案**

1.  适当调高NPU\_MEMORY\_FRACTION环境变量的值（代表内存分配比例，默认值为0.8），参考示例如下所示。

    ```
    export NPU_MEMORY_FRACTION=0.96 
    export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True 
    export OMP_NUM_THREADS=1
    ```

2.  适当调低服务化配置文件config.json中maxSeqLen、maxInputTokenLen、maxPrefillBatchSize、maxPrefillTokens、maxBatchSize等参数的值，主要是调整maxPrefillTokens、maxSeqLen和maxPrefillToken参数。
    -   maxPrefillTokens需要大于等于maxInputToken。
    -   maxPrefillTokens会影响到atb初始化阶段的workspace，其值过大时拉起服务后可能直接出现Out of memory报错。

3.  调整npuMemSize（代表单个NPU可以用来申请KV Cache的size上限，默认值为-1，表示自动分配KV Cahce；大于0表示手动分配，会根据设置的值固定分配KV Cache大小）参数值。

    npuMemSize = 单卡总内存 \* 内存分配比例

4.  使用更多显卡，比如当前使用2张卡，可以适当增加至4张或者8张，前提是需要确认当前模型在当前硬件中支持几张卡。

# 多模态模型推理时报错：Qwen2VL/Qwen2.5VL_VIT_graph nodes[1] infershape fail.

**问题描述**

多模态模型进行推理时出现类似以下报错提示：

```
[standard_model.py:188] : [Model] >>> global rank-2 Execute type:1, Exception:Qwen25VL_VIT_graph nodes[1] infershape  fail, enable log: export ASDOPS_LOG_LEVEL=ERROR, export ASDOPS_LOG_TO_STDOUT=1
```

或者：

```
[error] [1256320] [operation_base.cpp:273] Qwen25VL_VIT_layer_0_graph infer shape fail, error code: 8
```
<br>

**原因分析**

-   使用的模型在当前版本可能不支持该硬件环境。

-   输入shape过大，selfattention算子不支持。

<br>

**解决方案**

-   使用的模型在当前版本可能不支持该硬件环境
    -   单击[链接](https://www.hiascend.com/software/mindie/modellist)，查看MindIE各版本模型支持度，选择正确的MindIE版本。
    -   通过修改代码临时解决，可直接在镜像中修改相应的代码，只需要修改python代码，不需要重新编译，如下所示。

        ![](../figures/faq011.png)

-   输入shape过大，selfattention算子不支持

    将服务化配置文件config.json中的maxPrefillTokens参数适当调小。

# 多模态模型进行推理时，出现输入image_url/video_url/audio_url格式报错问题

**问题描述**

多模态模型输入image\_url/video\_url/audio\_url格式进行推理时，出现类似以下报错提示：

```
File "/usr/local/Ascend/atb-models/examples/models/qwen2_vl/run_pa.py", line 365, in <module>    raise TypeError("The multimodal input field currently only supports 'image' and 'video'.")
```
<br>

**原因分析**

image\_url/video\_url/audio\_url参数中的值未符合指定的要求。

<br>

**解决方案**

**Image**

1.  格式一：\{"type": "image\_url", "image\_url": image\_url\}， 此类格式的image\_url支持本地路径、jpg图片的base64编码、http和https协议url。

2. 格式二：\{"type": "image\_url", "image\_url": \{"url": \{image\_url\}\}\}，此类格式的image\_url支持本地路径、jpg图片的base64编码、http和https协议url。

3. 格式三：\{"type": "image\_url", "image\_url": \{"url": "file://\{local\_path\}"\}，此类格式仅支持本地路径。

4. 格式四：\{"type": "image\_url", "image\_url": \{"url": f"data:<mime\_type\>/<subtype\>;base64,<base64\_data\>"\}\}，此类格式仅支持base64编码，源格式可以为jpg、jpeg、png，对应的MIME如下表所示。

|格式|MIME|
|--|--|
|jpg|image/jpeg|
|jpeg|image/jpeg|
|png|image/png|


**Video**

1.  格式一：\{"type": "video\_url", "video\_url": video\_url\}， 此类格式的video\_url支持本地路径、http和https协议url。

2.  格式二：\{"type": "video\_url", "video\_url": \{"url": \{video\_url\}\}\}，此类格式的video\_url支持本地路径、http和https协议url。

3.  格式三：\{"type": "video\_url", "video\_url": \{"url": "file://\{local\_path\}"\}，此类格式仅支持本地路径。

4.  格式四：\{"type": "video\_url", "video\_url": \{"url": f"data:<mime\_type\>/<subtype\>;base64,<base64\_data\>"\}\}，此类格式仅支持base64编码，源格式可以为mp4、avi、wmv，对应的MIME如下表所示。另，由于视频编码的后的长度可能超出MindIE Service服务化请求字符长度的最大上限，因此并不建议使用base64编码格式传输视频。

|格式|MIME|
|--|--|
|mp4|video/mp4|
|avi|video/x-msvideo|
|wmv|video/x-ms-wmv|


**Audio**

1.  格式一：\{"type": "audio\_url", "audio\_url": audio\_url\}， 此类格式的audio\_url支持本地路径、http和https协议url。
2.  格式二：\{"type": "audio\_url", "audio\_url": \{"url": \{audio\_url\}\}\}，此类格式的audio\_url支持本地路径、http和https协议url。
3.  格式三：\{"type": "audio\_url", "audio\_url": \{"url": "file://\{local\_path\}"\}，此类格式仅支持本地路径。
4.  格式四：\{"type": "audio\_url", "audio\_url": \{"url": f"data:<mime\_type\>/<subtype\>;base64,<base64\_data\>"\}\}，此类格式仅支持base64编码，源格式可以为mp3、wav、flac，对应的MIME如下表所示。

|格式|MIME|
|--|--|
|mp3|audio/mpeg|
|wav|audio/x-wav|
|flac|audio/flac|


5.  格式五：\{"type": "input\_audio", "input\_audio": \{"data": f"\{audio\_base64\}", "format": "wav"\}\}，当type为input\_audio时，仅支持base64编码格式，源格式支持mp3、wav、flac，同时，必须通过format字段明确源格式类型。

# 运行Qwen2-VL系列模型时报错：Failed to get vocab size from tokenizer wrapper with exception...

**问题描述**

Qwen2-VL系列模型Tokenizer报错（其他模型也有可能报错，报错无关于模型），出现类似以下报错提示：

```
Failed to get vocab size from tokenizer wrapper with exception...
```

**图 1**  报错提示

![](../figures/faq012.png)

<br>

**原因分析**

-   模型适配的transformers/tokenizer版本不正确。
-   trust\_remote\_code参数配置为false。
-   服务化的config.json文件、模型权重路径和模型config.json文件权限不正确。
-   模型权重文件下可能缺少config.json文件。
-   词表文件损坏。

<br>

**解决方案**

-   模型适配的transformers/tokenizer版本不正确。
    -   确认每个模型需要安装依赖的transformers的版本，一般在模型文件的requirements.txt文件中可查看。然后查看模型权重路径下的config.json文件中的transformers版本号是否一致。
    -   使用以下tokenizer校验方法，创建一个Python脚本，如果运行成功，则tokenizer加载无问题。

        ```
        from transformers import AutoTokenizer  tokenizer = AutoTokenizer.from_pretrained('path/to/model')
        ```

-   trust\_remote\_code参数配置为false。
    -   将trust\_remote\_code参数配置为true。

-   服务化的config.json文件、模型权重路径和模型config.json文件权限不正确。
    -   将服务化的config.json文件、模型权重路径和模型config.json文件权限修改为640。

-   模型权重文件下可能缺少config.json文件。
    -   如果缺少config.json文件，将其补齐。

-   词表文件损坏。
    -   使用以下命令检查tokenizer.json文件的完整性

        ```
        sha256sum tokenizer.json # 哈希校验，输出的值和原权重文件进行对比
        ```

# MindIE部署Qwen2.5系列模型执行量化推理时报错

**问题描述**

MindIE部署Qwen2.5系列模型执行量化推理时出现以下报错信息：

```
ValueError：linear type not matched，please check 'config.json' 'quantize' parameter
```

或

```
AttributeError: 'ForkAwareLocal' object has no attribute 'connection‘
```

<br>

**原因分析**

未配置quantize字段。

<br>

**解决方案**

执行量化推理时，需要在量化权重所在路径的config.json文件中添加quantize字段，值为当前量化权重的量化方式，示例如下：

```
"quantize": "w8a8"
```

# 使用MindIE进行推理时，如何保证每次推理结果的一致性

**问题描述**

使用MindIE进行推理，相同输入，组batch顺序不同时，模型推理输出结果不同。

<br>

**原因分析**

由于matmul算子在不同行上的累加顺序不完全相同，且浮点精度没有加法交换律的特性，导致不同行上即使输入完全相同，计算结果也会存在一定的误差。

<br>

**解决方案**

确定性计算，是指在输入数据集等输入条件不变时，多次运行推理应用，输出结果每次保持一致。可以通过设置环境变量export ATB\_MATMUL\_SHUFFLE\_K\_ENABLE=0将加速库matmul的shuffle k功能关闭，关闭之后可以保证所有行上算子累加顺序一致，但matmul性能会下降10%左右 。

通信算子：

```
export LCCL_DETERMINISTIC=1 
export HCCL_DETERMINISTIC=true #开启归约类通信算子的确定性计算
export ATB_LLM_LCOC_ENABLE=0
```

MatMul：

```
export ATB_MATMUL_SHUFFLE_K_ENABLE=0
```

