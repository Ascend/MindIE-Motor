# 启动服务

## 环境准备

请参考[安装指南](./install/environment_preparation.md)进行环境的安装与部署，并参考[配置参数说明（服务化）](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/user_manual/service_parameter_configuration.md)根据用户需要配置参数。

## 操作步骤

- Server可以部署兼容Triton/OpenAI/TGI/vLLM第三方框架接口的服务应用。推荐用户开启HTTPS通信，并按照[单机部署](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/user_manual/prefill_decode_mixed_deployment.md)，配置开启HTTPS通信所需服务证书、私钥等证书文件。

- Server启动的默认IP地址和端口号为`https://127.0.0.1:1025`，用户可修改config.json文件中的"ipAddress"和"port"参数来配置启动IP地址与端口号。
- Server可实现服务状态查询，模型信息查询，文本/流式推理等功能。

>[!WARNING]警告
>HTTP缺乏必要的安全机制，容易受到数据泄露、数据篡改和中间人攻击的威胁，建议用户谨慎使用HTTP协议。

1. 两种启动服务方法如下所示。

    启动命令需在/*{MindIE安装目录}*/latest/mindie-service目录中执行。

    >[!NOTE]说明 
    >拉起服务前，建议用户使用MindStudio的预检工具进行配置文件字段校验，辅助校验配置的合法性，详情请参见[链接](https://gitcode.com/Ascend/msit/tree/master/msprechecker)。

    - 方式一（推荐）：使用后台进程方式启动服务。后台进程方式启动服务后，关闭窗口后进程也会保留。

        ```bash
        nohup ./bin/mindieservice_daemon > output.log 2>&1 &
        ```

        在标准输出流捕获到的文件中，打印如下信息说明启动成功。

        ```bash
        Daemon start success!
        ```

    - 方式二：直接启动服务。

        ```bash
        ./bin/mindieservice_daemon
        ```

        回显如下则说明启动成功。

        ```bash
        Daemon start success!
        ```

    >[!NOTE]说明
    >- bin目录按照安全要求，目录权限为550，没有写权限，但执行推理过程中，算子会在当前目录生成kernel_meta文件夹，需要写权限，因此不能直接在bin启动mindieservice_daemon。
    >- Ascend-cann-toolkit工具会在执行服务启动的目录下生成kernel_meta_temp_*xxxx*目录，该目录为算子的cce文件保存目录。因此需要在当前用户拥有写权限目录下（例如Ascend-mindie-server\_*\{version\}*\_linux-*{arch}* 目录，或者用户在Ascend-mindie-server\_*\{version\}*\_linux-*\{arch\}* 目录下自行创建临时目录）启动推理服务。
    >- 如需切换用户，请在切换用户后执行 **rm -f /dev/shm/** 命令，删除由之前用户运行创建的共享文件。避免切换用户后，该用户没有之前用户创建的共享文件的读写权限，造成推理失败。
    >- 标准输出流捕获到的文件output.log支持用户自定义文件和路径。
    >- 如果您使用的模型为超大模型（比如1300B的超大模型），其模型加载时间将会很长，请参见[加载大模型时耗时过长](https://gitcode.com/Ascend/MindIE-LLM/blob/master/docs/zh/faq/faq.md#%E5%8A%A0%E8%BD%BD%E5%A4%A7%E6%A8%A1%E5%9E%8B%E6%97%B6%E8%80%97%E6%97%B6%E8%BF%87%E9%95%BF)章节缩短加载时间。

2. 用户可使用HTTPS客户端（Linux curl命令，Postman工具等）发送HTTPS请求，此处以Linux curl命令为例进行说明。

    重新打开一个窗口，使用以下命令发送请求。例如列出当前模型列表：

    ```json
    curl -H "Accept: application/json" -H "Content-type: application/json" --cacert ca.pem --cert client.pem  --key client.key.pem -X GET https://127.0.0.1:1025/v1/models
    ```

    >[!NOTE]说明
    请用户根据实际情况对相应参数进行修改。
    >- --cacert：验签证书文件路径。
    >- ca.pem：Server服务端证书的验签证书/根证书。
    >- --cert：客户端证书文件路径。
    >- client.pem：客户端证书。
    >- --key：客户端私钥文件路径。
    >- client.key.pem：客户端证书私钥（未加密，建议采用加密密钥）。

# 服务化接口调用

## 使用vLLM兼容OpenAI接口

本章节以v1/chat流式推理接口和v1/completions流式推理接口为例介绍接口调用，其他接口的调用方法请参见[EndPoint业务面RESTful接口](https://www.hiascend.com/document/detail/zh/mindie/230/mindiellm/llmdev/mindie_service0065.html)章节。

**1. v1/chat流式推理接口**

<table><tbody><tr><th>接口名
</th>
<td>v1/chat流式推理接口
</td>
</tr>
<tr id="row1459719619535"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.2.1"><p id="p35979619535"><strong id="b859713619535">URL</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.2.1 "><p id="p175971268531"><strong>https://</strong><em>{服务IP地址}:{端口号}</em><strong >/v1/chat/completions</strong></p>
</td>
</tr>
<tr id="row1159818645317"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.3.1"><p id="p1859818685319"><a name="p1859818685319"></a><a name="p1859818685319"></a><strong id="b75981864538"><a name="b75981864538"></a><a name="b75981864538"></a>请求类型</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.3.1 "><p id="p1659812675319"><a name="p1659812675319"></a><a name="p1659812675319"></a><strong id="b8598467539"><a name="b8598467539"></a><a name="b8598467539"></a>POST</strong></p>
</td>
</tr>
<tr id="row859820665317"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.4.1"><p id="p6598568538"><a name="p6598568538"></a><a name="p6598568538"></a><strong id="b35980655316"><a name="b35980655316"></a><a name="b35980655316"></a>请求示例</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.4.1 "><pre class="screen" id="screen1159826205313">curl -H "Accept: application/json" -H "Content-type: application/json" --cacert ca.pem --cert client.pem  --key client.key.pem -X POST -d '{
    "model": "Qwen",
    "messages": [
        {
            "role": "user",
            "content": "You are a helpful assistant."
        }
    ],
    "stream": true,
    "presence_penalty": 1.03,
    "frequency_penalty": 1.0,
    "repetition_penalty": 1.0,
    "temperature": 0.5,
    "top_p": 0.95,
    "top_k": 1,
    "seed": 1,
    "max_tokens": 5,
    "n": 2,
    "best_of": 2
}' https://127.0.0.1:1025/v1/chat/completions</pre>
</td>
</tr>
<tr id="row1598176155312"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.5.1">返回示例
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.5.1 "><pre class="screen" id="screen1559816195311"></pre><a name="screen1559816195311"></a><a name="screen1559816195311"></a>data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":0,"delta":{"role":"assistant","content":"You"},"logprobs":null,"finish_reason":null}]}
data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":1,"delta":{"role":"assistant","content":"You"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":0,"delta":{"role":"assistant","content":" are"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":1,"delta":{"role":"assistant","content":" are"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":0,"delta":{"role":"assistant","content":" a"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":1,"delta":{"role":"assistant","content":" a"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":0,"delta":{"role":"assistant","content":" helpful"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","choices":[{"index":1,"delta":{"role":"assistant","content":" helpful"},"logprobs":null,"finish_reason":null}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","usage":{"prompt_tokens":24,"prompt_tokens_details": {"cached_tokens": 0},"completion_tokens":5,"total_tokens":29,"batch_size":[1,1,1,1,1],"queue_wait_time":[5318,117,82,72,196]},"choices":[{"index":0,"delta":{"role":"assistant","content":" assistant"},"logprobs":null,"finish_reason":"length"}]}

data: {"id":"endpoint_common_10","object":"chat.completion.chunk","created":1744038509,"model":"llama","usage":{"prompt_tokens":24,"prompt_tokens_details": {"cached_tokens": 0},"completion_tokens":5,"total_tokens":29,"batch_size":[1,1,1,1,1],"queue_wait_time":[5318,117,82,72,196]},"choices":[{"index":1,"delta":{"role":"assistant","content":" assistant"},"logprobs":null,"finish_reason":"length"}]}

data: [DONE]
</td>
</tr>
</tbody>
</table>

**2. v1/completions流式推理接口**

<table><tbody><tr id="row449917161206"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.1.1"><p id="p449961614018"><a name="p449961614018"></a><a name="p449961614018"></a><strong id="b04997161103"><a name="b04997161103"></a><a name="b04997161103"></a>接口名</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.1.1 "><p id="p1649921617014"><a name="p1649921617014"></a><a name="p1649921617014"></a>v1/completions流式推理接口</p>
</td>
</tr>
<tr id="row144991216607"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.2.1"><p id="p24991016905"><a name="p24991016905"></a><a name="p24991016905"></a><strong id="b249913165011"><a name="b249913165011"></a><a name="b249913165011"></a>URL</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.2.1 "><p><strong>https://</strong><em>{服务IP地址}:{端口号}</em><strong>/v1/completions</strong></p>
</td>
</tr>
<tr id="row1649917161011"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.3.1"><p id="p104994168014"><a name="p104994168014"></a><a name="p104994168014"></a><strong id="b13499171613014"><a name="b13499171613014"></a><a name="b13499171613014"></a>请求类型</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.3.1 "><p id="p19499316706"><a name="p19499316706"></a><a name="p19499316706"></a><strong id="b1049911163011"><a name="b1049911163011"></a><a name="b1049911163011"></a>POST</strong></p>
</td>
</tr>
<tr id="row124991161205"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.4.1"><p id="p4499416408"><a name="p4499416408"></a><a name="p4499416408"></a><strong id="b1349911164019"><a name="b1349911164019"></a><a name="b1349911164019"></a>请求示例</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.4.1 "><pre class="screen" id="screen114995168013"><a name="screen114995168013"></a><a name="screen114995168013"></a>curl -H "Accept: application/json" -H "Content-type: application/json" --cacert ca.pem --cert client.pem  --key client.key.pem -X POST -d '{
    "model": "Qwen2.5-7B-Instruct",
    "prompt": "who are you",
    "temperature": 1,
    "max_tokens": 5,
    "use_beam_search": true,
    "ignore_eos":true,
    "n": 2,
    "best_of":2,
    "stream": true,
    "logprobs": 2
}' https://127.0.0.1:1025/v1/completions</pre>
</td>
</tr>
<tr id="row949916161501"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.5.1"><p id="p144991116705"><a name="p144991116705"></a><a name="p144991116705"></a>返回示例</p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.5.1 "><pre class="screen" id="screen2499181615013"></pre><a name="screen2499181615013"></a><a name="screen2499181615013"></a>data: {"id":"endpoint_common_1","object":"text_completion","created":1744948803,"model":"Qwen2.5-7B-Instruct","choices":[{"index":0,"text":"\nI am a large","logprobs":{"text_offset":[0,1,2,5,7],"token_logprobs":[-1.8828125,-0.018310546875,-0.054931640625,-0.435546875,-0.0286865234375],"tokens":["\n","I"," am"," a"," large"],"top_logprobs":[{"\n":-1.8828125,"\n\n":-2.0},{"I":-0.018310546875,"Hello":-4.53125},{" am":-0.054931640625,"'m":-2.9375},{" a":-0.435546875," Q":-1.1875},{" large":-0.0286865234375," language":-4.78125}]},"stop_reason":null,"finish_reason":"length"},{"index":1,"text":"\n\nI am a large","logprobs":{"text_offset":[13,15,16,19,21],"token_logprobs":[-2.0,-0.031494140625,-0.0791015625,-0.5546875,-0.01092529296875],"tokens":["\n\n","I"," am"," a"," large"],"top_logprobs":[{"\n\n":-2.0,"\n":-1.8828125},{"I":-0.031494140625,"Hello":-4.28125},{" am":-0.0791015625,"'m":-2.578125},{" a":-0.5546875," Q":-1.0546875},{" large":-0.01092529296875," language":-6.375}]},"stop_reason":null,"finish_reason":"length"}],"usage":{"prompt_tokens":3,"prompt_tokens_details": {"cached_tokens": 0},"completion_tokens":10,"total_tokens":13,"batch_size":[1,1,1,1,1,1,1,1,1,1],"queue_wait_time":[5496,146,65,60,111,42,27,70,64,51]}}

data: [DONE]
</td>
</tr>
</tbody>
</table>

## 使用MindIE原生接口

本章节以文本推理接口和流式推理接口为例介绍接口调用，其他接口的调用方法请参见[EndPoint业务面RESTful接口](https://www.hiascend.com/document/detail/zh/mindie/230/mindiellm/llmdev/mindie_service0065.html)章节。

**1. 文本推理接口**

<table><tbody><tr id="row1167694011"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.1.1"><p id="p2067674413"><strong id="b667674719">接口名</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.1.1 "><p id="p1467610410115">文本推理接口</p>
</td>
</tr>
<tr id="row167610411112"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.2.1"><p id="p126761742120"><strong id="b267614113">URL</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.2.1 "><p id="p76761947119"><strong id="b467664619">https://</strong><em>{服务IP地址}:{端口号}</em><strong id="b16761046117">/infer</strong></p>
</td>
</tr>
<tr id="row9676141111"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.3.1"><p id="p86761241814"><a name="p86761241814"></a><a name="p86761241814"></a><strong id="b0676204110"><a name="b0676204110"></a><a name="b0676204110"></a>请求类型</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.3.1 "><p id="p15676049112"><a name="p15676049112"></a><a name="p15676049112"></a><strong id="b9676141911"><a name="b9676141911"></a><a name="b9676141911"></a>POST</strong></p>
</td>
</tr>
<tr id="row10676647119"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.4.1"><p id="p46761541812"><a name="p46761541812"></a><a name="p46761541812"></a><strong id="b1467610420110"><a name="b1467610420110"></a><a name="b1467610420110"></a>请求示例</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.4.1 "><pre class="screen" id="screen1767620410113"><a name="screen1767620410113"></a><a name="screen1767620410113"></a>curl -H "Accept: application/json" -H "Content-type: application/json" --cacert ca.pem --cert client.pem  --key client.key.pem -X POST -d '{
    "inputs": "My name is Olivier and I",
    "stream": false,
    "parameters": {
        "temperature": 0.5,
        "top_k": 10,
        "top_p": 0.95,
        "max_new_tokens": 20,
        "do_sample": true,
        "seed": null,
        "repetition_penalty": 1.03,
        "details": true,
        "typical_p": 0.5,
        "watermark": false,
        "priority": 5,
        "timeout": 10
    }
}' https://127.0.0.1:1025/infer</pre>
</td>
</tr>
<tr id="row196761641415"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.5.1"><p id="p1067612419114"><a name="p1067612419114"></a><a name="p1067612419114"></a>返回示例</p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.5.1 "><pre class="screen" id="screen1676174811"><a name="screen1676174811"></a><a name="screen1676174811"></a>{
    "generated_text": "am a French native speaker. I am looking for a job in the hospitality industry. I",
    "details": {
        "finish_reason": "length",
        "generated_tokens": 20,
        "seed": 846930886
    }
}</pre>
</td>
</tr>
</tbody>
</table>

**2. 流式推理接口**

<a name="table42361071315"></a>
<table><tbody><tr id="row192361871912"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.1.1"><p id="p1923657713"><a name="p1923657713"></a><a name="p1923657713"></a><strong id="b12360711115"><a name="b12360711115"></a><a name="b12360711115"></a>接口名</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.1.1 "><p id="p623627816"><a name="p623627816"></a><a name="p623627816"></a>流式推理接口</p>
</td>
</tr>
<tr id="row4236578110"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.2.1"><p id="p9236574112"><a name="p9236574112"></a><a name="p9236574112"></a><strong id="b223607917"><a name="b223607917"></a><a name="b223607917"></a>URL</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.2.1 "><p id="p2023612715113"><a name="p2023612715113"></a><a name="p2023612715113"></a><strong id="b62361371718"><a name="b62361371718"></a><a name="b62361371718"></a>https://</strong><em id="i4977163112433"><a name="i4977163112433"></a><a name="i4977163112433"></a>{服务IP地址}:{端口号}</em><strong id="b102361571018"><a name="b102361571018"></a><a name="b102361571018"></a>/infer</strong></p>
</td>
</tr>
<tr id="row162361571013"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.3.1"><p id="p14236207616"><a name="p14236207616"></a><a name="p14236207616"></a><strong id="b1823610719116"><a name="b1823610719116"></a><a name="b1823610719116"></a>请求类型</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.3.1 "><p id="p1236077110"><a name="p1236077110"></a><a name="p1236077110"></a><strong id="b152361578118"><a name="b152361578118"></a><a name="b152361578118"></a>POST</strong></p>
</td>
</tr>
<tr id="row723637211"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.4.1"><p id="p142361379118"><a name="p142361379118"></a><a name="p142361379118"></a><strong id="b162365711120"><a name="b162365711120"></a><a name="b162365711120"></a>请求示例</strong></p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.4.1 "><pre class="screen" id="screen1236278113"><a name="screen1236278113"></a><a name="screen1236278113"></a>curl -H "Accept: application/json" -H "Content-type: application/json" --cacert ca.pem --cert client.pem  --key client.key.pem -X POST -d '{
    "inputs": "My name is Olivier and I",
    "stream": true,
    "parameters": {
        "temperature": 0.5,
        "top_k": 10,
        "top_p": 0.95,
        "max_new_tokens": 20,
        "do_sample": true,
        "seed": null,
        "repetition_penalty": 1.03,
        "details": true,
        "typical_p": 0.5,
        "watermark": false,
        "priority": 5,
        "timeout": 10
    }
}' https://127.0.0.1:1025/infer</pre>
</td>
</tr>
<tr id="row132361071019"><th class="firstcol" valign="top" width="13.36%" id="mcps1.1.3.5.1"><p id="p9236871213"><a name="p9236871213"></a><a name="p9236871213"></a>返回示例</p>
</th>
<td class="cellrowborder" valign="top" width="86.64%" headers="mcps1.1.3.5.1 "><pre class="screen" id="screen13236274110"></pre><a name="screen13236274110"></a><a name="screen13236274110"></a>data: {"prefill_time":45.54,"decode_time":null,"token":{"id":[626],"text":"am"}}

data: {"prefill_time":null,"decode_time":128.32,"token":{"id":[263],"text":" a"}}

data: {"prefill_time":null,"decode_time":18.17,"token":{"id":[5176],"text":" French"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[17739],"text":" photograph"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[261],"text":"er"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[2729],"text":" based"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[297],"text":" in"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[3681],"text":" Paris"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[29889],"text":"."}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[13],"text":"\n"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[29902],"text":"I"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[505],"text":" have"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[1063],"text":" been"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[27904],"text":" shooting"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[1951],"text":" since"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[306],"text":" I"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[471],"text":" was"}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[29871],"text":" "}}

data: {"prefill_time":null,"decode_time":16.80,"token":{"id":[29896],"text":"1"}}

data: {"prefill_time":null,"decode_time":16.80,"generated_text":"am a French photographer based in Paris.\nI have been shooting since I was 15","details":{"finish_reason":"length","generated_tokens":20,"seed":846930886},"token":{"id":[29945],"text":null}}
</td>
</tr>
</tbody>
</table>

# 精度测试

目前MindIE支持AISBench工具进行精度测试，示例如下所示，其详细使用方法请参见[AISBench工具](https://gitee.com/aisbench/benchmark)。

**操作步骤**

1. 使用以下命令下载并安装AISBench工具。

    ```bash
    git clone https://gitee.com/aisbench/benchmark.git 
    cd benchmark/ 
    pip3 install -e ./ --use-pep517
    pip3 install -r requirements/api.txt 
    pip3 install -r requirements/extra.txt
    ```

    >[!NOTE]说明
    >pip安装方式适用于使用AISBench最新功能的场景（镜像安装MindIE方式除外）。AISBench工具已预装在MindIE镜像中，可使用以下命令查看AISBench工具在MindIE镜像中的安装路径。
    >
    >```bash
    >pip show ais_bench_benchmark
    >```

2. 准备数据集。

    以gsm8k为例，单击[gsm8k数据集](https://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/gsm8k.zip)下载数据集，将解压后的gsm8k文件夹放置于工具根路径的ais\_bench/datasets文件夹下。

3. 配置ais\_bench/benchmark/configs/models/vllm\_api/vllm\_api\_stream\_chat.py文件，示例如下所示。

    ```python
    from ais_bench.benchmark.models import VLLMCustomAPIChatStream  
    models = [     
        dict(         
            attr="service",
            type=VLLMCustomAPIChatStream,
            abbr='vllm-api-stream-chat',
            path="",                    # 指定模型序列化词表文件绝对路径，即是模型权重文件夹路径        
            model="DeepSeek-R1",        # 指定服务端已加载模型名称，依据实际VLLM推理服务拉取的模型名称配置（配置成空字符串会自动获取）        
            request_rate = 0,           # 请求发送频率，每1/request_rate秒发送1个请求给服务端，小于0.1则一次性发送所有请求        
            retry = 2,         
            host_ip = "localhost",      # 指定推理服务的IP        
            host_port = 8080,           # 指定推理服务的端口        
            max_out_len = 512,          # 推理服务输出的token的最大数量        
            batch_size=1,               # 请求发送的最大并发数        
            generation_kwargs = dict(
                temperature = 0.5,
                top_k = 10,
                top_p = 0.95,
                seed = None,
                repetition_penalty = 1.03,
            )     
        ) 
    ]
    ```

4. 执行以下命令启动服务化精度测试。

    ```bash
    ais_bench --models vllm_api_stream_chat --datasets demo_gsm8k_gen_4_shot_cot_chat_prompt --debug
    ```

    回显如下所示则表示执行成功：

    ```bash
    dataset                 version  metric   mode  vllm_api_general_chat 
    ----------------------- -------- -------- ----- ---------------------- 
    demo_gsm8k              401e4c   accuracy gen                   62.50
    ```

# 性能测试

目前MindIE支持AISBench工具进行性能测试，示例如下所示，其详细使用方法请参见[AISBench工具](https://gitee.com/aisbench/benchmark)。

**操作步骤**

1. 使用以下命令下载并安装AISBench工具。

    ```bash
    git clone https://gitee.com/aisbench/benchmark.git 
    cd benchmark/ 
    pip3 install -e ./ --use-pep517
    pip3 install -r requirements/api.txt 
    pip3 install -r requirements/extra.txt
    ```

    >[!NOTE]说明
    >pip安装方式适用于使用AISBench最新功能的场景（镜像安装MindIE方式除外）。AISBench工具已预装在MindIE镜像中，可使用以下命令查看AISBench工具在MindIE镜像中的安装路径。
    >
    >```bash
    >pip show ais_bench_benchmark
    >```

2. 准备数据集。   

    以gsm8k为例，单击[gsm8k数据集](https://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/gsm8k.zip)下载数据集，将解压后的gsm8k/文件夹放置于工具根路径的ais_bench/datasets文件夹下。

3. 配置ais_bench/benchmark/configs/models/vllm_api/vllm_api_stream_chat.py文件，示例如下所示。

    ```python
    from ais_bench.benchmark.models import VLLMCustomAPIChatStream  
    models = [     
        dict(         
            attr="service",         
            type=VLLMCustomAPIChatStream,         
            abbr='vllm-api-stream-chat',         
            path="",                    # 指定模型序列化词表文件绝对路径，即是模型权重文件夹路径        
            model="DeepSeek-R1",        # 指定服务端已加载模型名称，依据实际VLLM推理服务拉取的模型名称配置（配置成空字符串会自动获取）        
            request_rate = 0,           # 请求发送频率，每1/request_rate秒发送1个请求给服务端，小于0.1则一次性发送所有请求        
            retry = 2,         
            host_ip = "localhost",      # 指定推理服务的IP        
            host_port = 8080,           # 指定推理服务的端口        
            max_out_len = 512,          # 推理服务输出的token的最大数量        
            batch_size=1,               # 请求发送的最大并发数        
            generation_kwargs = dict(             
                temperature = 0.5,             
                top_k = 10,             
                top_p = 0.95,             
                seed = None,             
                repetition_penalty = 1.03,             
                ignore_eos = True,      # 推理服务输出忽略eos（输出长度会达到max_out_len）        
            )     
        ) 
    ]
    ```

4. 执行以下命令启动服务化性能测试。

    ```bash
    ais_bench --models vllm_api_stream_chat --datasets demo_gsm8k_gen_4_shot_cot_chat_prompt --mode perf --debug
    ```

    回显如下所示则表示执行成功：       

    ```linux
    ╒════════════╤════╤════════╤═══════╤══════╤═══════╤══════╤═══════╤═══════╤═══╕
    │ Performance Parameters │ Stage  │ Average        │ Min          │ Max        │ Median       │ P75        │ P90          │ P99          │ N    │ 
    │ E2EL                   │total   │ 2048.2945  ms  │ 1729.7498 ms │ 3450.96 ms │ 2491.8789 ms │ 2750.85 ms │ 3184.9186 ms │ 3424.4354 ms │ 8    │
    │ TTFT                   │total   │ 50.332 ms      │ 50.6244 ms   │ 52.0585 ms │ 50.3237 ms   │ 50.5872 ms │ 50.7566 ms   │ 50.0551 ms  │ 8    │
    │ TPOT                   │total   │ 10.6965 ms     │ 10.061 ms    │ 10.8805 ms │ 10.7495 ms   │ 10.7818 ms │ 10.808 ms    │ 10.8582 ms   │ 8    │ 
    │ ITL                    │total   │ 10.6965 ms     │ 7.3583 ms    │ 13.7707 ms │ 10.7513 ms   │ 10.8009 ms │ 10.8358 ms   │ 10.9322 ms   │ 8    │ 
    │ InputTokens            │total   │ 1512.5         │ 1481.0       │ 1566.0     │ 1511.5       │ 1520.25    │ 1536.6       │ 1563.06      │ 8    │ 
    │ OutputTokens           │total   │ 287.375        │ 200.0        │ 407.0      │ 280.0        │ 322.75     │ 374.8        │ 403.78       │ 8    │ 
    │ OutputTokenThroughput  │total   │ 115.9216       │ 107.6555     │ 116.5352   │ 117.6448     │ 118.2426   │ 118.3765     │ 118.6388     │ 8    │
    ╘════════════╧════╧════════╧═══════╧══════╧═══════╧══════╧═══════╧═══════╧═══╛
    ╒═════════════╤═════╤══════════╕
    │ Common Metric            │ Stage    │ Value              │ 
    │ Benchmark Duration       │ total    │ 19897.8505 ms      │ 
    │ Total Requests           │ total    │ 8                  │ 
    │ Failed Requests          │ total    │ 0                  │ 
    │ Success Requests         │ total    │ 8                  │ 
    │ Concurrency              │ total    │ 0.9972             │ 
    │ Max Concurrency          │ total    │ 1                  │ 
    │ Request Throughput       │ total    │ 0.4021 req/s       │ 
    │ Total Input Tokens       │ total    │ 12100              │ 
    │ Prefill Token Throughput │ total    │ 17014.3123 token/s │ 
    │ Total generated tokens   │ total    │ 2299               │ 
    │ Input Token Throughput   │ total    │ 608.7438 token/s   │ 
    │ Output Token Throughput  │ total    │ 115.7835 token/s   │ 
    │ Total Token Throughput   │ total    │ 723.5273 token/s   │ 
    ╘═════════════╧═════╧══════════╛
    ```

    性能测试结果主要关注TTFT、TPOT、Request Throughput和Output Token Throughput输出参数，参数详情信息请参见[表2 性能测试结果指标](service_oriented_optimization_tool.md#table_ptrm002)。

    >[!NOTE]说明
    >任务执行的过程会落盘在默认的输出路径，该输出路径在运行中的打印日志中有提示，日志内容如下所示：
    >
    >```linux
    >08/28 15:13:26 - AISBench - INFO - Current exp folder: outputs/default/20250828_151326
    >```
    >
    >命令执行结束后，outputs/default/20250828\_151326中的任务执行的详情如下所示：
    >
    >```linux
    >20250828_151326           # 每次实验基于时间戳生成的唯一目录 
    >├── configs               # 自动存储的所有已转储配置文件 
    >├── logs                  # 执行过程中日志，命令中如果加--debug，不会有过程日志落盘（都直接打印出来了） 
    >│   └── performance/      # 推理阶段的日志文件 
    >└── performance           # 性能测评结果 
    >│    └── vllm-api-stream-chat/          # “服务化模型配置”名称，对应模型任务配置文件中models的 abbr参数 
    >│         ├── gsm8kdataset.csv          # 单次请求性能输出（CSV），与性能结果打印中的Performance Parameters表格一致 
    >│         ├── gsm8kdataset.json         # 端到端性能输出（JSON），与性能结果打印中的Common Metric表格一致 
    >│         ├── gsm8kdataset_details.json # 全量打点日志（JSON） 
    >│         └── gsm8kdataset_plot.html    # 请求并发可视化报告（HTML）
    >```

# 停止服务

使用安装用户登录安装节点，两种停止Server服务方式如下所示。

- 方式一（推荐）：若使用后台进程方式启动服务，两种停止服务方式如下所示：
    - 使用kill命令停止进程。

        ```bash
        kill {mindieservice_daemon 进程id}
        ```

        >[!NOTE]说明
        >Linux系统中查询mindieservice_daemon主进程ID：
        >1. 查看所有与MindIE Motor相关的进程列表。
        >
        >    ```bash
        >    ps -ef | grep 'mindieservice_daemon'
        >    ```
        >
        >    回显示例如下：
        >
        >    ```linux
        >    (base) [xxxx@localhost test]# ps -ef | grep 'mindieservice_daemon'
        >    UID          PID    PPID  C STIME TTY          TIME CMD 
        >    xxxx     1595969  386706  5 11:37 pts/1    00:00:24 ./bin/mindieservice_daemon
        >    xxxx     1606004 1595969  0 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606005 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606006 1595969  0 11:42 pts/1    00:00:00 ./bin/mindieservice_daemon
        >    xxxx     1606007 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606009 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606010 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606012 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1606013 1595969  1 11:42 pts/1    00:00:01 ./bin/mindieservice_daemon
        >    xxxx     1616310  559909  0 11:44 pts/5    00:00:00 grep --color=auto mindieservice_daemon
        >    ```
        >
        >2. 在回显结果中找到PPID列，找出所有包含mindieservice_daemon且PPID相同的进程，这个相同的PPID指向的进程即为mindieservice_daemon主进程，它的PID即为主进程ID，回显示例中的主进程ID为：1595969。

    - 或使用pkill命令停止进程。

        ```bash
        pkill -9 mindie
        ```

- 方式二：若直接启动进程方式启动服务，可以通过按ctrl+c停止服务。
