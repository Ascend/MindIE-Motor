# TGI流式推理接口

**接口功能**

提供流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/generate_stream
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|inputs|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|parameters|可选|模型推理后处理相关参数。|-|
|decoder_input_details|可选|是否返回推理请求文本的token id。|bool类型，默认值false。对于流式接口，该参数只能为false。|
|details|可选|是否返回推理详细输出结果。根据TGI 0.9.4接口行为，details=true，即返回所有的details信息。|bool类型参数，默认值false。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|return_full_text|可选|是否将推理请求文本（inputs参数）添加到推理结果前面。|bool类型，默认值false。true表示添加。false表示不添加。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|uint32_t类型，取值范围(0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json或者padded_vocab_size的文件中读取的vocab_size值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0)，字段未设置时，默认值使用1.0来表示不进行该项处理，但是不可主动设置为1.0。|
|truncate|可选|输入文本做tokenizer之后，将token数量截断到该长度。读取截断后的n个token。若该字段值大于或等于token数量，则该字段无效。|uint32_t类型，取值范围(0, 2147483647]，字段未设置时，默认使用0来表示不进行该项处理，但是不可主动设置为0。|
|typical_p|可选|解码输出概率分布指数。当前后处理不支持。|float类型，取值范围(0.0, 1.0]，默认值1.0。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|stop|可选|停止推理的文本。输出结果默认不包含停止词列表文本。|List[string]类型或者string类型，默认值null。List[string]：列表元素不超过1024个，每个元素的字符长度为1~1024，列表元素总字符长度不超过32768（256*128）。列表为空时相当于null。string：字符长度范围为1~1024。PD分离场景暂不支持该参数。|
|adapter_id|可选|指定推理时使用的LoRA权重，即LoRA ID。|string类型，默认值"None"。由字母、数字、点、中划线、下划线和正斜杠组成，字符串长度小于或等于256。PD分离场景暂不支持该参数。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/generate_stream
```

请求消息体：

-   单模态文本模型：

    ```
    {
        "inputs": "My name is Olivier and I",
        "parameters": {
            "decoder_input_details": false,
            "details": true,
            "do_sample": true,
            "max_new_tokens": 20,
            "repetition_penalty": 1.03,
            "return_full_text": false,
            "seed": null,
            "temperature": 0.5,
            "top_k": 10,
            "top_p": 0.95,
            "truncate": null,
            "typical_p": 0.5,
            "watermark": false,
            "stop": null,
            "adapter_id": "None"
        }
    }
    ```

-   多模态模型：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "inputs": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
        "parameters": {
            "decoder_input_details": false,
            "details": true,
            "do_sample": true,
            "max_new_tokens": 20,
            "repetition_penalty": 1.03,
            "return_full_text": false,
            "seed": null,
            "temperature": 0.5,
            "top_k": 10,
            "top_p": 0.95,
            "truncate": null,
            "typical_p": 0.5,
            "watermark": false,
            "stop": null,
            "adapter_id": "None"
        }
    }
    ```

响应样例：

-   响应样例1（使用sse格式返回）：

    ```
    data: {"token":{"id":[13],"text":"\n","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[26626],"text":"Jan","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[300],"text":"et","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[3732],"text":" makes","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[395],"text":" $","logprob":null,"special":null},"generated_text":null,"details":null}
    
    ……
    
    data: {"token":{"id":[395],"text":" $","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[29896],"text":"1","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[29896],"text":"1","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[29947],"text":"8","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[29889],"text":".","logprob":null,"special":null},"generated_text":null,"details":null}
    
    ```

-   响应样例2（配置项"fullTextEnabled"=true，使用sse格式返回）：

    ```
    data: {"token":{"id":[198],"text":"\n","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[9707],"text":"\nHello","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[0],"text":"\nHello!","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[2585],"text":"\nHello! How","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[646],"text":"\nHello! How can","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[358],"text":"\nHello! How can I","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[7789],"text":"\nHello! How can I assist","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[498],"text":"\nHello! How can I assist you","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[3351],"text":"\nHello! How can I assist you today","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[30],"text":"\nHello! How can I assist you today?","logprob":null,"special":null},"generated_text":null,"details":null}
    
    data: {"token":{"id":[151645],"text":"\nHello! How can I assist you today?","logprob":null,"special":null},"generated_text":"\nHello! How can I assist you today?","details":{"prompt_tokens":1,"finish_reason":"eos_token","generated_tokens":11,"seed":2296576927}}
    ```

**输出说明**

|返回值|类型|说明|
|--|--|--|
|data|object|一次推理返回的结果。|
|token|object|每一次推理的token。|
|id|list|生成的token id组成的列表。|
|text|string|token对应文本。|
|logprob|对数概率|当前不支持，默认返回null。|
|special|bool|表明该token是否是special，如果是special=true，该token在做连接的时候可以被忽略。当前不支持，默认返回null。|
|generated_text|string|推理文本返回结果，只在最后一次推理结果才返回。|
|details|object|推理details结果，只在最后一次推理结果返回，并且请求参数details=true才返回details结果。|
|prompt_tokens|int|用户输入的prompt文本对应的token长度。|
|finish_reason|string|结束原因，只在最后一次推理结果返回。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|seed|int|返回推理请求的seed值，如果请求参数没有指定seed参数，则返回系统随机生成的seed值。|


# TGI文本推理接口

**接口功能**

提供文本推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port\}/generate
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|inputs|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|parameters|可选|模型推理后处理相关参数。|-|
|decoder_input_details|可选|是否返回推理请求文本的token id。|bool类型，默认值false。|
|details|可选|是否返回推理详细输出结果。根据TGI 0.9.4接口行为，decoder_input_details和details字段任意一个为true，即返回所有的details信息。|bool类型，默认值false。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|return_full_text|可选|是否将推理请求文本（inputs参数）添加到推理结果前面。|bool类型，默认值false。true表示添加。false表示不添加。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|uint32_t类型，取值范围(0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0)，字段未设置时，默认使用1.0来表示不进行该项处理，但是不可主动设置为1.0。|
|truncate|可选|输入文本做tokenizer之后，将token数量截断到该长度。读取截断后的n个token。若该字段值大于或等于token数量，则该字段无效。|uint32_t类型，取值范围(0, 2147483647]，字段未设置时，默认使用0来表示不进行该项处理，但是不可主动设置为0。|
|typical_p|可选|解码输出概率分布指数。当前后处理不支持。|float类型，取值范围(0.0, 1.0]，默认值1.0。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|stop|可选|停止推理的文本。输出结果默认不包含停止词列表文本。|List[string]类型或者string类型，默认值null。List[string]：列表元素不超过1024个，每个元素的字符长度为1~1024，列表元素总长度不超过32768（256*128）。列表为空时相当于null。string：字符长度范围为1~1024。PD分离场景暂不支持该参数。|
|adapter_id|可选|指定推理时使用的LoRA权重，即LoRA ID。|string类型，默认值"None"。由字母、数字、点、中划线、下划线和正斜杠组成，字符串长度小于或等于256。PD分离场景暂不支持该参数。|

<br>

**使用样例**
请求样例：

```
POST https://{ip}:{port}/generate
```

请求消息体：

-   单模态文本模型：

    ```
    {
        "inputs": "My name is Olivier and I",
        "parameters": {
            "decoder_input_details": true,
            "details": true,
            "do_sample": true,
            "max_new_tokens": 20,
            "repetition_penalty": 1.03,
            "return_full_text": false,
            "seed": null,
            "temperature": 0.5,
            "top_k": 10,
            "top_p": 0.95,
            "truncate": null,
            "typical_p": 0.5,
            "watermark": false,
            "stop": null,
            "adapter_id": "None"
        }
    }
    ```

-   多模态模式：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "inputs": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
        "parameters": {
            "decoder_input_details": true,
            "details": true,
            "do_sample": true,
            "max_new_tokens": 20,
            "repetition_penalty": 1.03,
            "return_full_text": false,
            "seed": null,
            "temperature": 0.5,
            "top_k": 10,
            "top_p": 0.95,
            "truncate": null,
            "typical_p": 0.5,
            "watermark": false,
            "stop": null,
            "adapter_id": "None"
        }
    }
    ```

响应样例：

```
{
    "details": {
        "finish_reason": "length",
        "generated_tokens": 1,
        "prefill": [{
            "id": 0,
            "logprob":null,
            "special": null,
            "text": "test"
        }],
        "prompt_tokens": 74,
        "seed": 42,
        "tokens": [{
            "id": 0,
            "logprob": null,
            "special": null,
            "text": "test"
        }]
    },
    "generated_text": "am a Frenchman living in the UK. I have been working as an IT consultant for "
}
```
<br>

**输出说明**

|返回值|类型|说明|
|--|--|--|
|details|object|推理details结果，请求参数decoder_input_details和details任意一个字段为true，即返回details结果。|
|finish_reason|string|结束原因。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|prefill|List[token]|请求参数decoder_input_details=true，返回推理请求文本detokenizer之后的token，默认为空列表。|
|`prefill.id`|int|token id。|
|prefill.logprob|float|对数概率，可以为空（第一个token概率值不能被计算获得）。当前不支持，默认返回null。|
|prefill.special|bool|表明该token是否是special，如果是special=true，该token在做连接的时候可以被忽略。当前不支持，默认返回null。|
|prefill.text|string|token对应文本。当前不支持，默认返回null。|
|prompt_tokens|int|用户输入的prompt文本对应的token长度。|
|seed|int|返回推理请求的seed值，如果请求参数没有指定seed参数，则返回系统随机生成的seed值。|
|tokens|List[token]|返回推理结果的所有tokens。|
|`tokens.id`|int|token id。|
|tokens.logprob|对数概率|当前不支持，默认返回null。|
|tokens.special|bool|表明该token是否是special，如果是special=true，该token在做连接的时候可以被忽略。当前不支持，默认返回null。|
|tokens.text|string|token对应文本。当前不支持，默认返回null。|
|generated_text|string|推理返回结果。|

# vLLM文本/流式推理接口

**接口功能**

提供文本/流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/generate
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|prompt|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|max_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值为Server配置文件中的maxIterTimes参数。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，取值范围(0.0, 2.0]，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。|
|presence_penalty|可选|存在惩罚介于-2.0和2.0之间，它影响模型如何根据到目前为止是否出现在文本中来惩罚新token。正值将通过惩罚已经使用的词，增加模型谈论新主题的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|frequency_penalty|可选|频率惩罚介于-2.0和2.0之间，它影响模型如何根据文本中词汇的现有频率惩罚新词汇。正值将通过惩罚已经频繁使用的词来降低模型一行中重复用词的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。1.0表示不进行计算，大于1.0表示输出随机性提高。temperature=0.0，即采用greedy sampling。|float类型，取值大于或等于0.0。取0.0时将忽略其他后处理参数做greedy search。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。-1表示不进行top k计算。|uint32_t类型，取值范围-1或者(0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。若传-1，-1会变为0传递给MindIE LLM后端，MindIE LLM后端会当做词表大小来处理。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|stream|可选|指定返回结果是文本推理还是流式推理。|bool类型，默认值false。true：流式推理。false：文本推理。|
|stop|可选|停止推理的文本。输出结果中默认不包含停止词列表文本。|List[string]类型或者string类型；默认为null。List[string]：每个元素字符长度大于或等于1，列表元素总字符长度不超过32768（32*1024）。列表为空时相当于null。string：字符长度范围为1~32768。PD分离场景暂不支持该参数。|
|stop_token_ids|可选|停止推理的token id列表。输出结果中默认不包含停止推理列表中的token id。|List[int32]类型，超出int32的元素将会被忽略。默认为null。|
|model|可选|指定推理时使用的LoRA权重，即LoRA ID。|string类型，默认值"None"。|
|include_stop_str_in_output|可选|决定是否在生成的推理文本中包含停止字符串。|bool类型，默认值为false。**PD场景暂不支持此参数**。true：包含停止字符串。false：不包含停止字符串。不传入stop或stop_token_ids时，此字段会被忽略。|
|skip_special_tokens|可选|指定在推理生成的文本中是否跳过特殊tokens。|bool类型，默认值为true。true：跳过特殊tokens。false：保留特殊tokens。|
|ignore_eos|可选|指定在推理文本生成过程中是否忽略eos_token结束符|bool类型，默认值为false。true：忽略eos_token结束符。false：不忽略eos_token结束符。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/generate
```

请求消息体：

-   单模态文本模型：

    ```
    {
        "prompt": "My name is Olivier and I",
        "max_tokens": 20,
        "repetition_penalty": 1.03,
        "presence_penalty": 1.2,
        "frequency_penalty": 1.2,
        "temperature": 0.5,
        "top_p": 0.95,
        "top_k": 10,
        "seed": null,
        "stream": false,
        "stop": null,
        "stop_token_ids": null,
        "model": "None",
        "include_stop_str_in_output": false,
        "skip_special_tokens": true,
        "ignore_eos": false
    }
    ```

-   多模态模型：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "prompt": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
        "max_tokens": 20,
        "repetition_penalty": 1.03,
        "presence_penalty": 1.2,
        "frequency_penalty": 1.2,
        "temperature": 0.5,
        "top_p": 0.95,
        "top_k": 10,
        "seed": null,
        "stream": false,
        "stop": null,
        "stop_token_ids": null,
        "model": "None",
        "include_stop_str_in_output": false,
        "skip_special_tokens": true,
        "ignore_eos": false
    }
    ```

响应样例：

-   文本推理（"stream"=false）：

    ```
    {"text":["My name is Olivier and I am a Frenchman living in the UK. I am a keen photographer and"]}
    ```

-   流式推理（"stream"=true，使用sse格式返回）：
    -   流式推理1（"stream"=true，使用sse格式返回）：

        ```
        {"text":["am"]}{"text":[" a"]}{"text":[" French"]}{"text":["man"]}{"text":[" living"]}{"text":[" in"]}{"text":[" the"]}{"text":[" UK"]}{"text":["."]}{"text":[" I"]} {"text":[" am"]}{"text":[" a"]}{"text":[" keen"]}{"text":[" photograph"]}{"text":["er"]}{"text":[" and"]}
        ```

    -   流式推理2（"stream"=true，配置项"fullTextEnabled"=true，使用sse格式返回）：

        ```
        {"text":[" to"]}{"text":[" to travel"]}{"text":[" to travel."]}{"text":[" to travel. I"]}{"text":[" to travel. I'm"]}
        ```

<br>

**输出说明**

|返回值|类型|说明|
|--|--|--|
|text|string|推理返回结果。|


>[!NOTE]说明
>vLLM流式返回结果，每个token的返回结果添加'\\0'字符做分割。使用**curl**命令发送vLLM流式推理请求的样例如下：
>```
>curl -H "Accept: application/json" -H "Content-type: application/json" --cacert /home/runs/static_conf/ca/ca.pem  --cert /home/runs/static_conf/cert/client.pem  --key /home/runs/static_conf/cert/client.key.pem -X POST -d '{
>"prompt": "My name is Olivier and I",
>"stream": true,
>"repetition_penalty": 1.0,
>"top_p": 1.0,
>"top_k": 10,
>"max_tokens": 16,
>"temperature": 1.0
>}' https://{ip}:{port}/generate | cat
>```

# OpenAI推理接口

>[!NOTE]说明
>- 运行环境的transformers版本不可低于4.34.1，低版本tokenizer不支持"chat_template"方法。
>- 推理模型权重路径下的tokenizer_config.json需要包含"chat_template"字段及其实现。
>- Function call功能的相关参数tool_call_id、tool_calls、tools和tool_choice当前仅支持部分模型，使用其他模型可能会报错。目前支持的模型只有ChatGLM3-6B和Qwen2.5系列。

**接口功能**

提供文本/流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v1/chat/completions
```

>[!NOTE]说明
>- {ip}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的{predict_ip}；如果没有配置该命令行参数，则取配置文件ms_coordinator.json的"predict_ip"参数。
>- {port}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的{predict_port}；如果没有配置该命令行参数，则取配置文件ms_coordinator.json的"predict_port"参数。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|model|必选|模型名。|与Server配置文件中modelName的取值保持一致。|
|messages|必选|推理请求消息结构。|list类型，0KB<messages内容包含的字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。|
|role|必选|推理请求消息角色。|字符串类型，可取角色有：<li>system：系统角色<li>user：用户角色<li>assistant：助手角色<li>tool：工具角色|
|content|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|<li>string：<br>-当role为assistant，且tool_calls非空时，content可以不传，其余角色非空。<br>-其余情况content非空。<li>list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|<li>text：文本<li>image_url：图片<li>video_url：视频<li>audio_url：音频<br>单个请求中image_url、video_url、audio_url数量总和<=20个。<br>**多媒体文件使用说明：** <br>本地文件方式：请将多媒体文件放置在以下目录：<br>/data/multimodal_inputs/<br>**安全风险提示：** 在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|tool_calls|可选|模型生成的工具调用。|类型为List[dict]，当role为assistant时，表示模型对工具的调用。|
|tool_calls.function|必选|表示模型调用的工具。|dict类型。<li>arguments，必选，使用JSON格式的字符串，表示调用函数的参数。<li>name，必选，字符串，调用的函数名。|
|tool_calls.id|必选|表示模型某次工具调用的ID。|字符串。|
|tool_calls.type|必选|调用的工具类型。|字符串，仅支持"function"。|
|tool_call_id|当role为tool时必选，否则可选|关联模型某次调用工具时的ID。|字符串。|
|stream|可选|指定返回结果是文本推理还是流式推理。|bool类型参数，默认值false。<li>true：流式推理。<li>false：文本推理。|
|presence_penalty|可选|存在惩罚介于-2.0和2.0之间，它影响模型如何根据到目前为止是否出现在文本中来惩罚新token。正值将通过惩罚已经使用的词，增加模型谈论新主题的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|frequency_penalty|可选|频率惩罚介于-2.0和2.0之间，它影响模型如何根据文本中词汇的现有频率惩罚新词汇。正值将通过惩罚已经频繁使用的词来降低模型一行中重复用词的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|repetition_penalty|可选|重复惩罚是一种技术，用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，取值范围(0.0, 2.0]，默认值1.0。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，取值范围[0.0, 2.0]，默认值1.0。<br>取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|int32类型，取值范围[0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。<br>vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围[0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。<br>当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|stop|可选|停止推理的文本。输出结果默认不包含停止词列表文本。|List[string]类型或者string类型，默认值null。<li>List[string]：列表元素不超过1024个，每个元素的字符长度为[1,1024]，列表元素总字符长度不超过32768（256*128）。列表为空时相当于null。<li>string：字符长度范围为[1,1024]。<br>PD分离场景暂不支持该参数。|
|stop_token_ids|可选|停止推理的token id列表。输出结果默认不包含停止推理列表中的token id。|List[int32]类型，超出int32的元素将会被忽略，默认值null。|
|include_stop_str_in_output|可选|决定是否在生成的推理文本中包含停止字符串。|bool类型，默认值false。**PD分离场景不支持此参数**。<li>true：包含停止字符串。<li>false：不包含停止字符串。<br>不传入stop或stop_token_ids时，此字段会被忽略。|
|skip_special_tokens|可选|指定在推理生成的文本中是否跳过特殊tokens。|bool类型，默认值true。<li>true：跳过特殊tokens。<li>false：保留特殊tokens。|
|ignore_eos|可选|指定在推理文本生成过程中是否忽略eos_token结束符。|bool类型，默认值false。<li>true：忽略eos_token结束符。<li>false：不忽略eos_token结束符。|
|max_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于min(maxIterTimes, max_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值为maxIterTimes的值。|
|tools|可选|可能会使用的工具列表。|List[dict]类型。|
|tools.type|必选|说明工具类型。|仅支持字符串"function"。|
|tools.function|必选|函数描述。|dict类型。|
|function.name|必选|函数名称。|字符串。|
|function.strict|可选|表示生成tool calls是否严格遵循schema格式。|bool类型，默认false。|
|function.description|可选|描述函数功能和使用。|字符串。|
|function.parameters|可选|表示函数接受的参数。|JSON schema格式。|
|parameters.type|必选|表示函数参数属性的类型。|字符串，仅支持object。|
|parameters.properties|必选|函数参数的属性。每一个key表示一个参数名，由用户自定义。value为dict类型，表示参数描述，包含type和description两个参数。|dict类型。|
|function.required|必选|表示函数必填参数列表。|List[string]类型。|
|function.additionalProperties|可选|是否允许使用未提及的额外参数。|bool类型，默认值false。<li>true：允许使用未提及的额外参数。<li>false：不允许使用未提及的额外参数。|
|tool_choice|可选|控制模型调用工具。|string类型或者dict类型，可以为null，默认值"auto"。<li>"none"：表示模型不会调用任何工具，而是生成一条消息。<li>"auto"：表示模型可以生成消息或调用一个或多个工具。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/chat/completions
```

请求消息体：

-   单轮对话：
    -   单模态模型：

        ```
        {
            "model": "deepseek",
            "messages": [{
                "role": "user",
                "content": "You are a helpful assistant."
            }],
            "stream": false,
            "presence_penalty": 1.03,
            "frequency_penalty": 1.0,
            "repetition_penalty": 1.0,
            "temperature": 0.5,
            "top_p": 0.95,
            "top_k": 0,
            "seed": null,
            "stop": ["stop1", "stop2"],
            "stop_token_ids": [2, 13],
            "include_stop_str_in_output": false,
            "skip_special_tokens": true,
            "ignore_eos": false,
            "max_tokens": 20
        }
        ```

    -   多模态模型：

        >[!NOTE]说明
        >"image\_url"参数的取值请根据实际情况进行修改。

        ```
        {
            "model": "qwen2.5_vl",
            "messages": [{
                "role": "user",
                "content": [
                   {"type": "text", "text": "My name is Olivier and I"},
                   {"type": "image_url", "image_url": "/xxxx/test.png"}
                ]
            }],
            "stream": false,
            "presence_penalty": 1.03,
            "frequency_penalty": 1.0,
            "repetition_penalty": 1.0,
            "temperature": 0.5,
            "top_p": 0.95,
            "top_k": 0,
            "seed": null,
            "stop": ["stop1", "stop2"],
            "stop_token_ids": [2, 13],
            "include_stop_str_in_output": false,
            "skip_special_tokens": true,
            "ignore_eos": false,
            "max_tokens": 20
        }
        ```

-   多轮对话：
    -   请求样例1：

        ```
        {
            "model": "deepseek",
            "messages": [{
                "role": "system",
                "content": "You are a helpful customer support assistant. Use the supplied tools to assist the user."
                },
                {
                "role": "user",
                "content": "Hi, can you tell me the delivery date for my order? my order id is 12345."
                }
            ],
            "stream": false,
            "presence_penalty": 1.03,
            "frequency_penalty": 1.0,
            "repetition_penalty": 1.0,
            "temperature": 0.5,
            "top_p": 0.95,
            "top_k": 0,
            "seed": null,
            "stop": ["stop1", "stop2"],
            "stop_token_ids": [2],
            "ignore_eos": false,
            "max_tokens": 1024,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_delivery_date",
                        "strict": true,
                        "description": "Get the delivery date for a customer's order. Call this whenever you need to know the delivery date, for example when a customer asks 'Where is my package'",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "order_id": {
                                    "type": "string",
                                    "description": "The customer's order ID."
                                }
                            },
                            "required": [
                                "order_id"
                            ],
                            "additionalProperties": false
                        }
                    }
                }
            ],
           "tool_choice": "auto"
        }
        ```

    -   请求样例2：

        ```
        {
            "model": "deepseek",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful customer support assistant. Use the supplied tools to assist the user."
                },
                {
                    "role": "user",
                    "content": "Hi, can you tell me the delivery date for my order? my order id is 12345."
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "arguments": "{\"order_id\": \"12345\"}",
                                "name": "get_delivery_date"
                            },
                            "id": "tool_call_8p2Nk",
                            "type": "function"
                        }
                    ]
                },
                {
                    "role": "tool",
                    "content": "the delivery date is 2024.09.10.",
                    "tool_call_id": "tool_call_8p2Nk"
                }
            ],
            "stream": false,
            "repetition_penalty": 1.1,
            "temperature": 0.9,
            "top_p": 1,
            "max_tokens": 1024,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_delivery_date",
                        "strict": true,
                        "description": "Get the delivery date for a customer's order. Call this whenever you need to know the delivery date, for example when a customer asks 'Where is my package'",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "order_id": {
                                    "type": "string",
                                    "description": "The customer's order ID."
                                }
                            },
                            "required": [
                                "order_id"
                            ],
                            "additionalProperties": false
                        }
                    }
                }
            ],
            "tool_choice": "auto"
        }
        ```

**响应样例：**

-   文本推理（"stream"=false）：
    -   单轮对话：

        ```
        {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1677652288,
            "model": "deepseek",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "\n\nHello there, how may I assist you today?"
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 12,
                "total_tokens": 21
            },
            "prefill_time": 200,
            "decode_time_arr": [56, 28, 28, 28, 32, 28, 28, 41, 28, 25, 28]
        }
        ```

    -   多轮对话：
        -   响应样例1：

            ```
            {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "created": 1677652288,
                "model": "deepseek",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "arguments": "{\"order_id\": \"12345\"}",
                                        "name": "get_delivery_date"
                                    },
                                    "id": "call_JwmTNF3O",
                                    "type": "function"
                                }
                            ]
                        },
                        "finish_reason": "tool_calls"
                    }
                ],
                "usage": {
                    "prompt_tokens": 226,
                    "completion_tokens": 122,
                    "total_tokens": 348
                },
                "prefill_time": 200,
                "decode_time_arr": [56, 28, 28, 28, 28, ..., 28, 32, 28, 28, 41, 28, 25, 28]
            }
            ```

        -   响应样例2：

            ```
            {
                "id": "endpoint_common_25",
                "object": "chat.completion",
                "created": 1728959154,
                "model": "deepseek",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "\n Your order with ID 12345 is scheduled for delivery on September 10th, 2024.",
                            "tool_calls": null
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": 265,
                    "completion_tokens": 30,
                    "total_tokens": 295
                }
                "prefill_time": 200,
                "decode_time_arr": [56, 28, 28, 28, 32, 28, 28, 41, 28, 25, 28, 28, 28, 28, 32, 28, 28, 41, 28, 25, 28, 28, 28, 28, 32, 28, 28, 41, 28]
            }
            ```

-   流式推理：
    -   流式推理1（"stream"=true，使用sse格式返回）：

        ```
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"\t"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"\t"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_8","object":"chat.completion.chunk","created":1729614610,"model":"llama3_70b","usage":{"prompt_tokens":54,"completion_tokens":17,"total_tokens":71},"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":"stop"}]}
        
        data: [DONE]
        ```

    -   流式推理2（"stream"=true，配置项"fullTextEnabled"=true，使用sse格式返回）：

        ```
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello!"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I assist"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I assist you"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I assist you today"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I assist you today?"},"finish_reason":null}]}
        
        data: {"id":"endpoint_common_11","object":"chat.completion.chunk","created":1730184192,"model":"llama3_70b","full_text":"Hello! How can I assist you today?","usage":{"prompt_tokens":31,"completion_tokens":10,"total_tokens":41},"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello! How can I assist you today?"},"finish_reason":"length"}]}
        
        data: [DONE]
        ```

<br>

**输出说明**

**表 1**  文本推理结果说明

|参数名|类型|说明|
|--|--|--|
|id|string|请求ID。|
|object|string|返回结果类型目前都返回"chat.completion"。|
|created|integer|推理请求时间戳，精确到秒。|
|model|string|使用的推理模型。|
|choices|list|推理结果列表。|
|index|integer|choices消息index，当前只能为0。|
|message|object|推理消息。|
|role|string|角色，目前都返回"assistant"。|
|content|string|推理文本结果。|
|tool_calls|list|模型工具调用输出。|
|function|dict|函数调用说明。|
|arguments|string|调用函数的参数，JSON格式的字符串。|
|name|string|调用的函数名。|
|tool_calls.id|string|模型调用工具的ID。|
|type|string|工具的类型，目前仅支持function。|
|finish_reason|string|结束原因。<li>stop：<br>-请求被CANCEL或STOP，用户不感知，丢弃响应。<br>-请求执行中出错，响应输出为空，err_msg非空。<br>-请求输入校验异常，响应输出为空，err_msg非空。<br>-请求遇eos结束符正常结束。<li>length：<br>-请求因达到最大序列长度而结束，响应为最后一轮迭代输出。<br>-请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。<li>tool_calls：表示模型调用了工具。|
|usage|object|推理结果统计数据。|
|prompt_tokens|int|用户输入的prompt文本对应的token长度。|
|completion_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中completion_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|total_tokens|int|请求和推理的总token数。|
|prefill_time|float|推理首token时延。<br>当生成多个序列的情况时，为所有序列的首token时延。|
|decode_time_arr|list|推理Decode时延数组。<br>当生成多个序列的情况时，为所有序列共同的Decode时延，时延数组长度为最长序列的Decode token数量。|


**表 2**  流式推理结果说明

|参数名|类型|说明|
|--|--|--|
|data|object|一次推理返回的结果。|
|id|string|请求ID。|
|object|string|目前都返回"chat.completion.chunk"。|
|created|integer|推理请求时间戳，精确到秒。|
|model|string|使用的推理模型。|
|full_text|string|全量文本结果，配置项fullTextEnabled=true时才有此返回值。|
|usage|object|推理结果统计数据。|
|prompt_tokens|int|用户输入的prompt文本对应的token长度。|
|completion_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中completion_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|total_tokens|int|请求和推理的总token数。|
|choices|list|流式推理结果。|
|index|integer|choices消息index，当前只能为0。|
|delta|object|推理返回结果，最后一个响应为空。|
|role|string|角色，目前都返回"assistant"。|
|content|string|推理文本结果。|
|finish_reason|string|结束原因，只在最后一次推理结果返回。<li>stop：<br>-请求被CANCEL或STOP，用户不感知，丢弃响应。<br>-请求执行中出错，响应输出为空，err_msg非空。<br>-请求输入校验异常，响应输出为空，err_msg非空。<br>-请求遇eos结束符正常结束。<li>length：<br>-请求因达到最大序列长度而结束，响应为最后一轮迭代输出。<br>-请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。|




# vLLM兼容OpenAI接口（v1/completions）

>[!NOTE]说明
>-   运行环境的transformers版本不可低于4.34.1，低版本tokenizer不支持"chat\_template"方法。
>-   推理模型权重路径下的tokenizer\_config.json需要包含"chat\_template"字段及其实现。
>-   Function Call功能的相关参数tool\_call\_id、tool\_calls、tools和tool\_choice当前仅支持部分模型，使用其他模型可能会报错。目前支持的模型只有ChatGLM3-6B。

**接口功能**

提供文本/流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v1/completions
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|model|必选|模型名。|与Server配置文件中modelName的取值保持一致。|
|prompt|必选|推理请求消息结构。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。|
|best_of|可选|推理生成best_of个序列。与n联合使用时，表现为从best_of个序列中，选取n个返回给用户。|**该参数后续版本即将日落。**int类型，取值范围为[1, 128]，默认值为1。当与参数n联合使用时，选取概率最高的前n个；流式推理场景下n和best_of必须相等。未设置参数best_of但设置参数n时，best_of将自动设置为n。该参数不支持与SplitFuse、Prefix Cache、并行解码、PD分离等特性叠加使用。|
|n|可选|推理生成n个序列。|int类型，取值范围为[1, 128]，默认值为1。可与best_of联合使用；流式推理场景下n和best_of相等。未设置参数best_of但设置参数n时，best_of将自动设置为n。该参数不支持与SplitFuse、Prefix Cache、并行解码、PD分离等特性叠加使用。|
|logprobs|可选|推理结果中每个token携带的logprobs的数量。|int类型，取值范围为[0, 5]，默认为null。该参数不支持与SplitFuse、Prefix Cache、并行解码、PD分离等特性叠加使用。|
|max_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值为Server配置文件中的maxIterTimes参数。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|stop|可选|停止推理的文本。输出结果中默认不包含停止词列表文本。|List[string]类型或者string类型；默认为null。List[string]：每个元素字符长度大于或等于1，列表元素总字符长度不超过32768（32*1024）。列表为空时相当于null。string：字符长度范围为1~32768。PD分离场景暂不支持该参数。|
|stream|可选|指定返回结果是文本推理还是流式推理。|bool类型，默认值false。true：流式推理。false：文本推理。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，取值范围(0.0, 2.0]，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。|
|presence_penalty|可选|存在惩罚介于-2.0和2.0之间，它影响模型如何根据到目前为止是否出现在文本中来惩罚新token。正值将通过惩罚已经使用的词，增加模型谈论新主题的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|frequency_penalty|可选|频率惩罚介于-2.0和2.0之间，它影响模型如何根据文本中词汇的现有频率惩罚新词汇。正值将通过惩罚已经频繁使用的词来降低模型一行中重复用词的可能性。|float类型，取值范围[-2.0, 2.0]，默认值0.0。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。1.0表示不进行计算，大于1.0表示输出随机性提高。temperature=0.0，即采用greedy sampling。|float类型，取值大于或等于0.0。取0.0时将忽略其他后处理参数做greedy search。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|uint32_t类型，取值范围-1或者(0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|stop_token_ids|可选|停止推理的token id列表。输出结果中默认不包含停止推理列表中的token id。|List[int32]类型，超出int32的元素将会被忽略。默认为null。|
|include_stop_str_in_output|可选|决定是否在生成的推理文本中包含停止字符串。|bool类型，默认值为false。**PD分离场景暂不支持此参数**。true：包含停止字符串。false：不包含停止字符串。不传入stop或stop_token_ids时，此字段会被忽略。|
|ignore_eos|可选|指定在推理文本生成过程中是否忽略eos_token结束符|bool类型，默认值为false。true：忽略eos_token结束符。false：不忽略eos_token结束符。|
|skip_special_tokens|可选|指定在推理生成的文本中是否跳过特殊tokens。|bool类型，默认值为true。true：跳过特殊tokens。false：保留特殊tokens。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/completions
```

请求消息体：

-   单模态模型（不带n和logprobs参数）：

    ```
    {
        "prompt": "How is your holiday?",
        "model": "llama3-70b",
        "max_tokens": 20,
        "stream": false,
        "frequency_penalty": 1.0,
        "presence_penalty": 1.1,
        "repetition_penalty": 1.2,
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 100,
        "ignore_eos": false,
        "stop_token_ids": [2, 3],
        "stop": [""],
        "include_stop_str_in_output": false,
        "seed":1,
        "skip_special_tokens": true
     }
    ```

-   单模态模型（带n和logprobs参数）：

    ```
    {
        "prompt": "How is your holiday?",
        "model": "llama3-70b",
        "max_tokens": 2,
        "best_of": 5,
        "n" : 3,
        "logprobs": 1
    }
    ```

响应样例：

-   不带n和logprobs参数

    ```
    {
        "id": "endpoint_common_8",
        "object": "text_completion",
        "created": 1740496393,
        "model": "llama3-70b",
        "choices": [
            {
                "index": 0,
                "text": " Hope the festive period has been a safe and happy one for you all. Today I’ll be bringing",
                "stop_reason": null,
                "finish_reason": "length"
            }
        ],
        "usage": {
            "prompt_tokens": 6,
            "completion_tokens": 20,
            "total_tokens": 26
        }
    }
    ```

-   带n和logprobs参数：

    ```
    {
        "id": "endpoint_common_10",
        "object": "text_completion",
        "created": 1740496661,
        "model": "llama3-70b",
        "choices": [
            {
                "index": 0,
                "text": " I hope",
                "logprobs": {
                    "text_offset": [
                        0,
                        2
                    ],
                    "token_logprobs": [
                        -1.4375,
                        -1.234375
                    ],
                    "tokens": [
                        " I",
                        " hope"
                    ],
                    "top_logprobs": [
                        {
                            " I": -1.4375
                        },
                        {
                            " hope": -1.234375
                        }
                    ]
                },
                "stop_reason": null,
                "finish_reason": "length"
            },
            {
                "index": 1,
                "text": " I hope",
                "logprobs": {
                    "text_offset": [
                        7,
                        9
                    ],
                    "token_logprobs": [
                        -1.4375,
                        -1.234375
                    ],
                    "tokens": [
                        " I",
                        " hope"
                    ],
                    "top_logprobs": [
                        {
                            " I": -1.4375
                        },
                        {
                            " hope": -1.234375
                        }
                    ]
                },
                "stop_reason": null,
                "finish_reason": "length"
            },
            {
                "index": 2,
                "text": " I hope",
                "logprobs": {
                    "text_offset": [
                        14,
                        16
                    ],
                    "token_logprobs": [
                        -1.4375,
                        -1.234375
                    ],
                    "tokens": [
                        " I",
                        " hope"
                    ],
                    "top_logprobs": [
                        {
                            " I": -1.4375
                        },
                        {
                            " hope": -1.234375
                        }
                    ]
                },
                "stop_reason": null,
                "finish_reason": "length"
            }
        ],
        "usage": {
            "prompt_tokens": 6,
            "completion_tokens": 10,
            "total_tokens": 16
        }
    }
    ```

**输出说明**

|返回值|类型|说明|
|--|--|--|
|id|string|请求ID。|
|object|string|表示返回对象的类型，通常是text_completion，表示这是一个文本生成的结果。|
|created|int|生成文本的时间戳，单位为秒。|
|model|string|使用的模型推理名称。|
|choices|array|推理结果列表。|
|index|int|每个选择的索引，表示该选择在列表中的位置（从0开始）。|
|text|string|模型生成的文本内容。|
|logprobs|object|包含有关该选择的详细logprobs信息。|
|stop_reason|string|表示生成停止的原因。为null表示没有显式的停止原因。|
|finish_reason|string|表示生成停止的原因，常见值为length（因为生成达到了最大长度）或stop（遇到停止符号）。|
|text_offset|list(int[])|表示生成文本的位置偏移量。|
|token_logprobs|list(float[])|对应生成文本中每个token的对数概率值。|
|tokens|list(string[])|生成的token。|
|top_logprobs|list(object[])|每个token的可能性，通常表示生成文本的最可能token和对应的logprobs。|
|usage|object|包含了有关请求中使用的令牌（tokens）的统计信息。令牌是模型输入和输出的基本单元。|
|prompt_tokens|int|输入文本（提示语）所使用的令牌数量。|
|completion_tokens|int|模型生成的文本（响应）的令牌数量。|
|total_tokens|int|请求中总共使用的令牌数量（prompt_tokens + completion_tokens）。|

# Triton流式推理接口

**接口功能**

提供流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]/generate_stream
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。
>-   $\{MODEL\_NAME\}字段指定需要查询的模型名称。
>-   [/versions/${MODEL_VERSION}]字段暂不支持，不传递。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|id|可选|请求ID。|长度不超过256的非空字符串。只允许包含下划线、中划线、大写英文字母、小写英文字母和数字。|
|text_input|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|parameters|可选|模型推理后处理相关参数。|-|
|details|可选|是否返回推理详细输出结果。|bool类型，默认值false。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|int32_t类型，取值范围[0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|batch_size|可选|推理请求batch_size|uint32_t类型，取值范围(0，2147483647]，默认值1。|
|typical_p|可选|解码输出概率分布指数。当前后处理不支持。|float类型，取值范围(0.0, 1.0]，字段未设置时，默认使用-1.0来表示不进行该项处理，但是不可主动设置为-1.0。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|perf_stat|可选|是否打开性能统计。|bool类型，默认值false。true：打开性能统计。false：不打开性能统计。|
|priority|可选|设置请求优先级。|uint64_t类型，取值范围[1, 5]，默认值5。值越低优先级越高，最高优先级为1。|
|timeout|可选|设置等待时间，超时则断开请求。|uint64_t类型，取值范围(0, 3600]，默认值600，单位：秒。|

<br>

**使用样例**

请求样例：

```
POST https://{ip}:{port}/v2/models/llama3-70b/generate_stream
```

请求消息体：

-   单模态文本模型：

    ```
    {
        "id":"a123",
        "text_input": "My name is Olivier and I",
        "parameters": {
            "details": true,
            "do_sample": true,
            "max_new_tokens":5,
            "repetition_penalty": 1.1,
            "seed": 123,
            "temperature": 1,
            "top_k": 10,
            "top_p": 0.99,
            "batch_size":100,
            "typical_p": 0.5,
            "watermark": false,
            "perf_stat": true,
            "priority": 5,
            "timeout": 10
        }
    }
    ```

-   多模态模型：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "id":"a123",
        "text_input": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
        "parameters": {
            "details": true,
            "do_sample": true,
            "max_new_tokens":20,
            "repetition_penalty": 1.1,
            "seed": 123,
            "temperature": 1,
            "top_k": 10,
            "top_p": 0.99,
            "batch_size":100,
            "typical_p": 0.5,
            "watermark": false,
            "perf_stat": false,
            "priority": 5,
            "timeout": 10
        }
    }
    ```

响应样例：

-   响应样例1：

    ```
    data:{"id":"a123","model_name":"llama3-70b","model_version":null,"text_output":"live","details":{"generated_tokens":1,"first_token_cost":null,"decode_cost":null,"perf_stat":[[5735,28]],"batch_size":1,"queue_wait_time":5082},"prefill_time":28,"decode_time":null}
    
    data:{"id":"a123","model_name":"llama3-70b","model_version":null,"text_output":" in","details":{"generated_tokens":2,"first_token_cost":null,"decode_cost":null,"perf_stat":[[5735,28],[297,9]],"batch_size":1,"queue_wait_time":36},"prefill_time":null,"decode_time":9}
    
    data:{"id":"a123","model_name":"llama3-70b","model_version":null,"text_output":" Paris","details":{"generated_tokens":3,"first_token_cost":null,"decode_cost":null,"perf_stat":[[5735,28],[297,9],[3681,8]],"batch_size":1,"queue_wait_time":30},"prefill_time":null,"decode_time":8}
    
    data:{"id":"a123","model_name":"llama3-70b","model_version":null,"text_output":",","details":{"generated_tokens":4,"first_token_cost":null,"decode_cost":null,"perf_stat":[[5735,28],[297,9],[3681,8],[29892,7]],"batch_size":1,"queue_wait_time":23},"prefill_time":null,"decode_time":7}
    
    data:{"id":"a123","model_name":"llama3-70b","model_version":null,"text_output":" France","details":{"finish_reason":"length","generated_tokens":5,"first_token_cost":null,"decode_cost":null,"perf_stat":[[5735,28],[297,9],[3681,8],[29892,7],[3444,7]],"batch_size":1,"queue_wait_time":24},"prefill_time":null,"decode_time":7}
    ```


<br>    

**输出说明**

|返回值|类型|说明|
|--|--|--|
|data|object|一次推理返回的结果。|
|id|string|请求ID。|
|model_name|string|模型名称。|
|model_version|string|模型版本。|
|text_output|string|推理返回结果。|
|finish_reason|string|推理结束原因，只在最后一次推理结果返回。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|details|object|推理details结果。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|first_token_cost|List[token]|文本推理返回，首token产生时间，单位：ms，当前未统计该数据，返回null。|
|decode_cost|int|decode时间，单位：ms，当前未统计该数据，返回null。|
|perf_stat|二维数组|数组元素：第一个值为tokenId。第二个值为推理出该tokenId所花费的时间。|
|batch_size|int|流式推理batch size。|
|queue_wait_time|int|队列等待时间，单位：us。|
|prefill_time|float|首token时延，单位：ms。|
|decode_time|float|非首token的token时延，单位：ms。|

# Triton Token推理接口

**接口功能**

提供Token推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]/infer
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。
>-   $\{MODEL\_NAME\}字段指定需要查询的模型名称。
>-   [/versions/${MODEL_VERSION}]字段暂不支持，不传递。

<br>

**请求参数**
|参数|是否必选|说明|取值范围|
|--|--|--|--|
|id|可选|推理请求ID。|长度不超过256的非空字符串。只允许包含下划线、中划线、大写英文字母、小写英文字母和数字。|
|inputs|必选|只有一个元素的数组。|长度为1。|
|inputs.name|必选|输入名称，固定"input0"。|字符串长度小于或等于256。|
|inputs.shape|必选|参数维度，一维时代表data长度，二维时代表1行n列，n为data长度。|data长度范围为(0, min(1024*1024, maxInputTokenLen, maxSeqLen-1, max_position_embeddings)]。其中max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。|
|inputs.datatype|必选|data数据类型，目前场景仅支持UINT32，传递tokenid。|UINT32。|
|inputs.data|必选|数组，代表输入的tokenId值。|data长度和shape中传入的data长度一致。tokenId的值需要在模型词表范围内。|
|outputs|必选|推理结果输出结构。|outputs长度需要和inputs的长度保持一致。|
|outputs.name|必选|推理结果输出名。|字符串。|
|parameters|可选|模型推理后处理相关参数。|-|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|uint32_t类型，取值范围[0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|details|可选|是否返回推理详细输出结果。此配置项为Triton文本推理所使用参数，在Triton Token推理无效，不建议传输。|bool类型，默认值false。|
|perf_stat|可选|是否打开性能统计。此配置项为Triton文本推理所使用参数，在Triton Token推理无效，不建议传输。|bool类型，默认值false。true：打开性能统计。false：不打开性能统计。|
|batch_size|可选|推理请求batch_size。此配置项为Triton文本推理所使用参数，在Triton Token推理无效，不建议传输。|uint32_t类型，取值范围(0，2147483647]，默认值1。|
|priority|可选|设置请求优先级。|uint64_t类型，取值范围[1, 5]，默认值5。值越低优先级越高，最高优先级为1。|
|timeout|可选|设置等待时间，超时则断开请求。|uint64_t类型，取值范围(0, 3600]，默认值600，单位：秒。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v2/models/deepseek/infer
```

请求消息体：

```
{
    "id": "42",
    "inputs": [{
        "name": "input0",
        "shape": [
            1,
            10
        ],
        "datatype": "UINT32",
        "data": [
            396, 319, 13996, 29877, 29901, 29907, 3333, 20718, 316, 23924
        ]
    }],
    "outputs": [{
        "name": "output0"
    }],
    "parameters": {
        "temperature": 0.5,
        "top_k": 10,
        "top_p": 0.95,
        "do_sample": true,
        "seed": null,
        "repetition_penalty": 1.03,
        "max_new_tokens": 20,
        "watermark": true,
        "priority": 5,
        "timeout": 10
    }
}
```

响应样例：

```
{
    "id": "42",
    "outputs": [
        {
            "name": "output0",
            "shape": [
                1,
                20
            ],
            "datatype": "UINT32",
            "data": [
                1,
                396,
                319,
                13996,
                29877,
                29901,
                29907,
                3333,
                20718,
                316,
                23924,
                562,
                2142,
                1702,
                425,
                14015,
                16060,
                316,
                383,
                19498
            ]
        }
    ]
}
```

<br>

**输出说明**

|返回值|类型|说明|
|--|--|--|
|id|string|请求ID。|
|outputs|list|推理结果列表。|
|name|string|默认"output0"。|
|shape|list|结构为[1, n]，1表示1维数组，n表示data字段中token结果长度。|
|datatype|string|"UINT32"。|
|data|list|推理后生成的token id集合。|

# Triton文本推理接口

**接口功能**

提供文本推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v2/models/${MODEL_NAME}[/versions/${MODEL_VERSION}]_/generate
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。
>-   $\{MODEL\_NAME\}字段指定需要查询的模型名称。
>-   [/versions/${MODEL_VERSION}]字段暂不支持，不传递。

<br>

**请求参数**

|参数|是否必选|说明|取值要求|
|--|--|--|--|
|id|可选|请求ID。|长度不超过256的非空字符串。只允许包含下划线、中划线、大写英文字母、小写英文字母和数字。|
|text_input|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|parameters|可选|模型推理后处理相关参数。|-|
|details|可选|是否返回推理详细输出结果。|bool类型，默认值false。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|int32_t类型，取值范围[0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0]，默认值1.0。|
|batch_size|可选|推理请求batch_size。|int32_t类型，取值范围(0，2147483647]，默认值1。|
|typical_p|可选|解码输出概率分布指数。当前后处理不支持。|float类型，取值范围(0.0, 1.0]，字段未设置时，默认使用-1.0来表示不进行该项处理，但是不可主动设置为-1.0。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|perf_stat|可选|是否打开性能统计。|bool类型，默认值false。true：打开性能统计。false：关闭性能统计。|
|priority|可选|设置请求优先级。|uint64_t类型，取值范围[1, 5]，默认值5。值越低优先级越高，最高优先级为1。|
|timeout|可选|设置等待时间，超时则断开请求。|uint64_t类型，取值范围(0, 3600]，默认值600，单位：秒。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v2/models/deepseek/generate
```

请求消息体：

-   单模态文本模型：

    ```
    {
        "id":"a123",
        "text_input": "My name is Olivier and I",
        "parameters": {
            "details": true,
            "do_sample": true,
            "max_new_tokens":5,
            "repetition_penalty": 1.1,
            "seed": 123,
            "temperature": 1,
            "top_k": 10,
            "top_p": 0.99,
            "batch_size":100,
            "typical_p": 0.5,
            "watermark": false,
            "perf_stat": true,
            "priority": 5,
            "timeout": 10
        }
    }
    ```

-   多模态模型：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "id":"a123",
        "text_input": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
        "parameters": {
            "details": true,
            "do_sample": true,
            "max_new_tokens":20,
            "repetition_penalty": 1.1,
            "seed": 123,
            "temperature": 1,
            "top_k": 10,
            "top_p": 0.99,
            "batch_size":100,
            "typical_p": 0.5,
            "watermark": false,
            "perf_stat": false,
            "priority": 5,
            "timeout": 10
        }
    }
    ```

响应样例：

```
{
    "id": "a123",
    "model_name": "deepseek",
    "model_version": null,
    "text_output": "live in Paris, France",
    "details": {
        "finish_reason": "length",
        "generated_tokens": 5,
        "first_token_cost": null,
        "decode_cost": null,
        "perf_stat": [
            [
                5735,
                17
            ],
            [
                297,
                8
            ],
            [
                3681,
                7
            ],
            [
                29892,
                7
            ],
            [
                3444,
                7
            ]
        ]
    }
}
```

<br>

**输出说明**

|返回值|类型|说明|
|--|--|--|
|id|string|请求ID。|
|model_name|string|模型名称。|
|model_version|string|模型版本。当前未统计该数据，返回null。|
|text_output|string|推理返回结果。|
|details|object|推理details结果。|
|finish_reason|string|推理结束原因。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|first_token_cost|List[token]|文本推理返回，首token产生时间，单位：ms，当前未统计该数据，返回null。|
|decode_cost|int|decode时间，单位：ms，当前未统计该数据，返回null。|
|perf_stat|二维数组|数组元素：第一个值为tokenId。第二个值为推理出该tokenId所花费的时间。|

# MindIE原生文本/流式推理接口

**接口功能**

提供MindIE原生文本/流式推理处理功能。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/infer
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

<br>

**请求参数**

|参数名|是否必选|说明|取值要求|
|--|--|--|--|
|inputs|必选|推理请求内容。单模态文本模型为string类型，多模态模型为list类型。|string：非空，0KB<字符数<=4MB，支持中英文。prompt经过tokenizer之后的token数量小于或等于maxInputTokenLen、maxSeqLen-1、max_position_embeddings和1MB之间的最小值。其中，max_position_embeddings从权重文件config.json中获取，其他相关参数从配置文件中获取。list：请参见使用样例中多模态模型样例。|
|type|可选|推理请求内容类型。|text：文本image_url：图片video_url：视频audio_url：音频单个请求中image_url、video_url、audio_url数量总和<=20个。**多媒体文件使用说明：****HTTP/HTTPS 访问方式**：请先配置白名单环境变量 ALLOWED_MEDIA_URLS_ENV。示例如下：export ALLOWED_MEDIA_URLS_ENV="http://<服务器地址>/xxx.jpg"**本地文件方式**：请将多媒体文件放置在以下目录：/data/multimodal_inputs/**安全风险提示：**在使用前，请务必确保传入的多媒体文件来源可信、内容安全，有效预防潜在安全风险。|
|text|可选|推理请求内容为文本。|非空，支持中英文。|
|image_url|可选|推理请求内容为图片。|支持服务器本地路径的图片传入，图片类型支持jpg、png、jpeg和base64编码的jpg图片，支持URL图片传入，支持HTTP和HTTPS协议。当前支持传入的图片最大为20MB。|
|video_url|可选|推理请求内容为视频。|支持服务器本地路径的视频传入，视频类型支持MP4、AVI、WMV，支持URL视频传入，支持HTTP和HTTPS协议。当前支持传入的视频最大512MB。|
|audio_url|可选|推理请求内容为音频。|支持服务器本地路径的音频传入，音频类型支持MP3、WAV、FLAC，支持URL音频传入，支持HTTP和HTTPS协议。当前支持传入的音频最大20MB。|
|stream|可选|指定返回结果是文本推理还是流式推理。|bool类型，默认值false。true：流式推理。false：文本推理。|
|parameters|可选|模型推理后处理相关参数。|-|
|temperature|可选|控制生成的随机性，较高的值会产生更多样化的输出。|float类型，大于1e-6，默认值1.0。取值越大，结果的随机性越大。推荐使用大于或等于0.001的值，小于0.001可能会导致文本质量不佳。建议最大值取2.0，同时视模型而定。|
|top_k|可选|控制模型生成过程中考虑的词汇范围，只从概率最高的k个候选词中选择。|uint32_t类型，取值范围(0, 2147483647]，字段未设置时，默认值由后端模型确定，详情请参见说明。取值大于或等于vocabSize时，默认值为vocabSize。vocabSize是从modelWeightPath路径下的config.json文件中读取的vocab_size或者padded_vocab_size的值，若不存在则vocabSize取默认值0。建议用户在config.json文件中添加vocab_size或者padded_vocab_size参数，否则可能导致推理失败。|
|top_p|可选|控制模型生成过程中考虑的词汇范围，使用累计概率选择候选词，直到累积概率超过给定的阈值。该参数也可以控制生成结果的多样性，它基于累积概率选择候选词，直到累积概率超过给定的阈值为止。|float类型，取值范围(1e-6, 1.0)，字段未设置时，默认使用1.0来表示不进行该项处理，但是不可主动设置为1.0。|
|max_new_tokens|可选|允许推理生成的最大token个数。实际产生的token数量同时受到配置文件maxIterTimes参数影响，推理token个数小于或等于Min(maxIterTimes, max_new_tokens)。|uint32_t类型，取值范围(0，2147483647]，默认值20。|
|do_sample|可选|是否做sampling。|bool类型，不传递该参数时，将由其他后处理参数决定是否做sampling。true：做sampling。false：不做sampling。|
|seed|可选|用于指定推理过程的随机种子，相同的seed值可以确保推理结果的可重现性，不同的seed值会提升推理结果的随机性。|uint64_t类型，取值范围(0, 18446744073709551615]，不传递该参数，系统会产生一个随机seed值。当seed取到临近最大值时，会有WARNING，但并不会影响使用。若想去掉WARNING，可以减小seed取值。|
|repetition_penalty|可选|重复惩罚用于减少在文本生成过程中出现重复片段的概率。它对之前已经生成的文本进行惩罚，使得模型更倾向于选择新的、不重复的内容。|float类型，大于0.0，默认值1.0。小于1.0表示对重复进行奖励。1.0表示不进行重复度惩罚。大于1.0表示对重复进行惩罚。建议最大值取2.0，同时视模型而定。|
|details|可选|是否返回推理详细输出结果。|bool类型，默认值false。|
|typical_p|可选|解码输出概率分布指数。当前后处理不支持。|float类型，取值范围(0.0, 1.0]，默认值1.0。|
|watermark|可选|是否带模型水印。当前后处理不支持。|bool类型，默认值false。true：带模型水印。false：不带模型水印。|
|priority|可选|设置请求优先级。|uint64_t类型，取值范围[1, 5]，默认值5。值越低优先级越高，最高优先级为1。|
|timeout|可选|设置等待时间，超时则断开请求。|uint64_t类型，取值范围(0, 3600]，默认值600，单位：秒。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/infer
```

请求消息体：

-   单模态文本模型：

    ```
    {
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
    }
    ```

-   多模态模型：

    >[!NOTE]说明
    >"image\_url"参数的取值请根据实际情况进行修改。

    ```
    {
        "inputs": [
            {"type": "text", "text": "My name is Olivier and I"},
            {
                "type": "image_url",
                "image_url": "/xxxx/test.png"
            }
        ],
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
    }
    ```

响应样例：

-   文本推理（"stream"=false）：

    ```
    {
        "generated_text": "am a French native speaker. I am looking for a job in the hospitality industry. I",
        "details": {
            "finish_reason": "length",
            "generated_tokens": 20,
            "seed": 846930886
        }
    }
    ```

-   流式推理：
    -   流式推理1（"stream"=true，使用sse格式返回）：

        ```
        data: {"prefill_time":45.54,"decode_time":null,"token":{"id":[626],"text":"am"}}
        
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
        ```

<BR>

**输出说明**

**表 1**  文本推理结果说明

|返回值|类型|说明|
|--|--|--|
|generated_text|string|推理返回结果。|
|details|object|推理details结果。目前定义以下字段，支持扩展。|
|finish_reason|string|推理结束原因。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|seed|int|如果请求指定了sampling seed，返回该seed值。|


**表 2**  流式推理结果说明

|返回值|类型|说明|
|--|--|--|
|data|object|一次推理返回的结果。|
|prefill_time|float|流式推理下首token时延，单位：ms。|
|decode_time|float|流式推理下非首token的token时延，单位：ms。|
|generated_text|string|推理文本结果，只在最后一次推理结果才返回。|
|details|object|推理details结果，只在最后一次推理结果返回，支持扩展。|
|finish_reason|string|推理结束原因，只在最后一次推理结果返回。eos_token：请求正常结束。stop_sequence：请求被CANCEL或STOP，用户不感知，丢弃响应。请求执行中出错，响应输出为空，err_msg非空。请求输入校验异常，响应输出为空，err_msg非空。length：请求因达到最大序列长度而结束，响应为最后一轮迭代输出。请求因达到最大输出长度（包括请求和模型粒度）而结束，响应为最后一轮迭代输出。invalid flag：无效标记。|
|generated_tokens|int|推理结果token数量。PD场景下统计P和D推理结果的总token数量。当一个请求的推理长度上限取maxIterTimes的值时，D节点响应中generated_tokens数量为maxIterTimes+1，即增加了P推理结果的首token数量。|
|seed|int|如果请求指定了sampling seed，返回该seed值。|
|token|List[token]|每一次推理的token。|
|id|list|生成的token id组成的列表。|
|text|string|token对应文本。|

# Token计算接口

**接口功能**

将文本转换为token。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v1/tokenizer
```

>[!NOTE]说明
>-   \{ip\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_ip"参数。
>-   \{port\}优先取[启动命令](../cluster_management_component/coordinator.md#section733210894016)参数中的\{predict\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"predict\_port"参数。

<br>

**请求参数**

|参数|取值要求|说明|
|--|--|--|
|inputs|string|必填。输入文本。|

<br>


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/tokenizer
```

请求消息体：

```
{
    "inputs": "what is your name?"
}
```

响应样例：

```
{
    "token_number": 6,
    "tokens": ["what", "is", "your", "name", "?"]
}
```

**输出说明**

|参数|类型|说明|
|--|--|--|
|token_number|size_t|文本转换为token的个数。|
|tokens|string[]|每个token的值。|








