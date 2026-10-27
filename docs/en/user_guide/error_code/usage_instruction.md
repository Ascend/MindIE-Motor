# Usage Instructions

An error code is presented as 12 characters. Taking the error code MIE01E000001 as an example, the meaning and description of each segment are shown in [Table 1](#table119819438205) and [Table 2](#table1121312172367).

**Table 1**<a name="table119819438205"></a> Meaning of Each Error Code Segment

|Field 1|Field 2|Field 3|Field 4|
|--|--|--|--|
|Product name (3 characters)|Component (2 digits)|Level (1 character)|Error code (6 digits)|
|MIE|01|E|000001|

**Table 2**<a name="table1121312172367"></a> Description of Each Error Code Segment

|Error Code Field|Description|
|--|--|
|MIE|Abbreviation of MindIE|
|01|Component code. The component codes are as follows:<ul><li>00 MindIE Cert</li><li>01 MindIE Benchmark</li><li>02 MindIE Client</li><li>03 MindIE Motor</li><li>04 Server</li><li>05 MindIE LLM</li><li>06 MindIE SD</li><li>07 MindIE RT</li><li>08 MindIE Torch</li></ul>|
|E|Error|
|000001|Internal hexadecimal error code of the component|
