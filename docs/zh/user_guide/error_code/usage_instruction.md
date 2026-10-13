# 使用说明

错误码以12位字符形式体现，以MIE01E000001错误码为例，各段含义及说明如[表1](#table119819438205)及[表2](#table1121312172367)所示。

**表 1**<a name="table119819438205"></a> 错误码各段含义

|字段1|字段2|字段3|字段4|
|--|--|--|--|
|产品名称（3位）|组件（2位）|级别（1位）|错误码（6位）|
|MIE|01|E|000001|

**表 2**<a name="table1121312172367"></a> 错误码各段说明

|错误码字段|说明|
|--|--|
|MIE|MindIE的缩写|
|01|组件编码，各组件编码如下：<ul><li>00 MindIE Cert</li><li>01 MindIE Benchmark</li><li>02 MindIE Client</li><li>03 MindIE Motor</li><li>04 Server</li><li>05 MindIE LLM</li><li>06 MindIE SD</li><li>07 MindIE RT</li><li>08 MindIE Torch</li></ul>|
|E|Error|
|000001|为组件内部16进制错误码|
