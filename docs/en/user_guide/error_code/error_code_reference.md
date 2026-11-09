# MindIE Motor Error Code Reference

## Error Code Display Format Description

MindIE Motor components use a 6-bit error code in hexadecimal format. Taking 100201 as an example, the meaning of each segment is shown in [Table 1](#table172676258115).

**Table 1**<a name='table172676258115'></a> Description of error code segments

|Error Code Field|Description|
|--|--|
|10 (first 2 digits)|Subcomponent code. For subcomponent codes, see:<br/><ul><li>10: ms-controller</li><li>20: ms-coordinator</li><li>30: ms-deployer</li><li>40: ms-common (including httpServer and others)</li></ul>|
|02 (middle 2 digits)|Feature code.|
|01 (last 2 digits)|Error type. For error type codes, see:<br/><ul><li>00: EXCEPTION. An exception is caught. For the specific error, see the printed exception information.</li><li>01: INVALID_INPUT. A parameter in the external input is invalid.</li><li>02: INVALID_PARAMETER. Parameter validation failed; the parameter is not from external input.</li><li>03: OUT_OF_RANGE. Out of range; the attempted operation exceeds the valid range.</li><li>04: OPERATION_REPEAT. Duplicate operation. The entity to be created or inserted (for example, a file or a directory) already exists, or the entity to be operated on (for example, to set a state) has already been operated on.</li><li>05: NOT_FOUND. Some entities (for example, files, directories, requests, or methods) are not found.</li><li>06: RESOURCE_LIMIT. A resource value reaches the configured threshold (maximum or minimum).</li><li>07: RESOURCE_EXHAUSTED. Resources are used up. Physical resources, such as CPU, disk space, and memory space, are overloaded. For example, memory allocation fails.</li><li>08: DEADLINE_EXCEEDED. The deadline has expired. The operation times out, or the network link is delayed (time_out).</li><li>09: UNREACHABLE. The communication link is interrupted.</li><li>0A: CALL_ERROR. An external error caused by a failure in the internal logic of the executed function. The causes vary, and you need to refer to the error information in the executed function.</li><li>0B: UNAVAILABLE. The service or resource is not supported or is unavailable (in terms of state or function).</li><li>0C: FAILED_PRECONDITION. The current system state rejects the operation. For example, the directory to be deleted is not empty.</li><li>0D: PENDING. The operation is interrupted, possibly due to a concurrency problem or an abnormal exit.</li><li>0E: DATA_LOSS. Unrecoverable data loss or corruption.</li><li>0F: UNAUTHENTICATED. The request does not have valid authentication credentials.</li><li>10: PERMISSION_DENIED. No operation permission.</li></ul>|

## MIE03E400008

**Fault Symptom**

HTTPServer communication timed out, connection establishment failed, and request execution failed.

**Fault Cause**

The wait time exceeds the configured latency.

**Fault Location**

Check whether the requested service has been started.

**Recommended Debugging Method**

Try to execute the request again, or restart the service.

## MIE03E400002

**Fault Symptom**

HTTPServer parameter validation failed, and the HTTPServer of the corresponding subcomponent failed to start.

**Fault Cause**

An error occurred when setting the context validation mode.

**Fault Location**

Check which parameters failed validation after the keyword "failed" in the fault log, and try to locate the problem based on the parameters.

## MIE03E400009

**Fault Symptom**

HTTPServer communication failed, connection establishment failed, and request execution failed.

**Fault Cause**

The communication link is interrupted.

**Fault Location**

Check the Boost status code of the failed connection establishment, and locate the cause based on the status code.

## MIE03E400006

**Fault Symptom**

HTTPServer resources are limited, and connection establishment failed.

**Fault Cause**

The number of HTTPServer connections reaches the configured upper limit.

**Fault Location**

Check the number of connections and whether there are duplicate connections.

## MIE03E400007

**Fault Symptom**

HttpServer failed to allocate memory during initialization, and the HttpServer of the corresponding subcomponent failed to start.

**Fault Cause**

Memory allocation failed during HttpServer initialization, which is generally caused by a failure to create a session or a listener.

**Fault Location**

Check whether memory resources are exhausted.

## MIE03E400005

**Fault Symptom**

The HttpServer configuration file does not exist, and the HttpServer of the corresponding subcomponent failed to start.

**Fault Cause**

The certificate or key file is missing in HttpServer.

**Fault Location**

Check whether the certificate path is correctly configured.

## MIE03E400001

**Fault Symptom**

The HTTPServer configuration items are invalid. As a result, the HTTPServer of the corresponding subcomponent failed to start, and the subcomponent failed to start.

**Fault Cause**

The key added in HTTPServer is invalid.

**Fault Location**

Check whether the certificate file is correct.

## MIE03E100005,MIE03E100305,MIE03E100405,MIE03E100505,MIE03E100705,MIE03E100805,MIE03E100905,MIE03E100B05,MIE03E100D05,MIE03E100F05

**Fault Symptom**

The Controller cannot find the requested resource, and the request execution fails.

**Fault Cause**

The resource (such as a node or a request) is not found.

**Fault Location**

Check the request parameters reported in the log.

## MIE03E100007,MIE03E100207,MIE03E100307,MIE03E100407,MIE03E100707,MIE03E100907,MIE03E100C07,MIE03E100D07,MIE03E101007

**Fault Symptom**

The Controller failed to allocate memory, and the Controller failed to start.

**Fault Cause**

Memory allocation failed in the Controller component, which may be caused by insufficient system resources leading to a failure in creating objects or threads.

**Fault Location**

Check whether memory resources are exhausted.

## MIE03E10000A,MIE03E10010A,MIE03E10060A,MIE03E10070A,MIE03E10090A,MIE03E100B0A,MIE03E100C0A,MIE03E100F0A,MIE03E10100A

**Fault Symptom**

Internal logic execution failed in the Controller, and business execution failed.

**Fault Cause**

This is a fault log triggered by an internal logic failure. For the specific fault cause, see the lower-layer logs nearby.

**Fault Location**

Please submit an ISSUE.

## MIE03E100404,MIE03E100C04

**Fault Symptom**

A Controller resource was operated on repeatedly, and the request execution failed.

**Fault Cause**

A node is registered repeatedly, or a URL handler is registered repeatedly.

**Fault Location**

Refer to the request parameters reported in the log, or submit an issue.

## MIE03E100501,MIE03E100601,MIE03E100701,MIE03E100801,MIE03E100901,MIE03E100A01,MIE03E100C01,MIE03E100D01

**Fault Symptom**

The Controller input configuration is invalid, and the Controller component failed to start.

**Fault Cause**

Some configuration items in the input configuration file do not exist or are invalid.

**Fault Location**

Check the configuration items reported as errors in the log.

## MIE03E100500,MIE03E100700,MIE03E100900,MIE03E100A00,MIE03E100B00,MIE03E100D00,MIE03E100F00

**Fault Symptom**

The Controller caught an exception, and business execution failed.

**Fault Cause**

This is generally an exception caught during JSON and string conversion.

**Fault Location**

Check the thrown exception information, and submit an ISSUE.

## MIE03E100702,MIE03E100802,MIE03E100B02,MIE03E100F02

**Fault Symptom**

Controller parameter validation failed, and request execution failed.

**Fault Cause**

Validation of the returned response failed, or a null value exception occurred.

**Fault Location**

Check the response information or validation information in the error log.

## MIE03E10070B,MIE03E10090B,MIE03E100F0B

**Fault Symptom**

The current Controller resource is unavailable, and the request execution failed.

**Fault Cause**

The node status is unavailable, or the method or hardware type is not supported.

**Fault Location**

Check the printed information such as the resource ID, IP, or type.

## MIE03E100706,MIE03E100906,MIE03E100D06

**Fault Symptom**

Controller resources are limited, and request execution failed.

**Fault Cause**

The number of nodes or the number of groups exceeds the configured upper limit.

**Fault Location**

Try to query the current Group and Node information, or submit an ISSUE.

## MIE03E100709,MIE03E100B09,MIE03E100E09,MIE03E100F09

**Fault Symptom**

Controller communication failed, and request execution failed.

**Fault Cause**

Sending an HTTP request within the Controller component failed, possibly due to an interrupted connection or an invalid request body.

**Fault Location**

Try to execute the request again, or restart the service.

## MIE03E20000A,MIE03E20010A,MIE03E20030A,MIE03E20050A,MIE03E20060A,MIE03E200B0A,MIE03E200C0A,MIE03E20170A,MIE03E20190A

**Fault Symptom**

Internal logic failed in the Coordinator, and business execution failed.

**Fault Cause**

This is a fault log triggered by an internal logic failure. For the specific fault cause, see the lower-layer logs.

**Fault Location**

Please submit an ISSUE.

## MIE03E200101,MIE03E200501,MIE03E200601,MIE03E200901,MIE03E200A01,MIE03E200C01,MIE03E201301,MIE03E201701,MIE03E201801

**Fault Symptom**

The Coordinator input configuration is invalid, and the Coordinator failed to start.

**Fault Cause**

Some configuration items in the configuration file input by the Coordinator do not exist or are invalid.

**Fault Location**

Check the configuration items reported as errors in the log, and modify the reported `user_config.json` configuration items as required by [Full Parameter Description of the user_config.json Configuration File](../configuration/config_reference.md#motor_coordinator_config).

## MIE03E200100,MIE03E200600,MIE03E200700,MIE03E200A00,MIE03E200B00,MIE03E200C00,MIE03E201400

**Fault Symptom**

The Coordinator caught an exception, and business execution failed.

**Fault Cause**

This is usually an exception during JSON processing at initialization, a cluster instance exception, or a request exception.

**Fault Location**

Try to locate the problem based on the thrown exception information, or submit ISSUE.

## MIE03E200204,MIE03E200C04,MIE03E201404

**Fault Symptom**

A Coordinator resource is operated on repeatedly, and the request execution fails.

**Fault Cause**

The cluster management module adds the same instance ID or request ID repeatedly.

**Fault Location**

Internal resource requests are executed repeatedly. Submit an ISSUE.

## MIE03E200305,MIE03E200505,MIE03E200605,MIE03E200A05,MIE03E200C05,MIE03E200D05,MIE03E200F05,MIE03E201005,MIE03E201105,MIE03E201205,MIE03E201305,MIE03E201405,MIE03E201505,MIE03E201605,MIE03E201905

**Fault Symptom**

The Coordinator cannot find the requested resource, and the request execution fails.

**Fault Cause**

Resources such as the node, connection, or request are not found.

**Fault Location**

Please submit an ISSUE.

## MIE03E200409,MIE03E200509,MIE03E200609,MIE03E200709,MIE03E200809,MIE03E200B09,MIE03E200C09,MIE03E200E09,MIE03E200F09,MIE03E201409,MIE03E201509,MIE03E201609

**Fault Symptom**

Coordinator communication failed, and request execution failed.

**Fault Cause**

The HTTP request body is invalid, or communication failed.

**Fault Location**

Attempt to retry the request, or restart the service.

## MIE03E200506,MIE03E200F06

**Fault Symptom**

Coordinator resources are limited, and request execution failed.

**Fault Cause**

The HttpClientAsync connection upper limit or the request retry count upper limit is reached.

**Fault Location**

Attempt to execute the request again, or restart the service.

## MIE03E200502,MIE03E200602,MIE03E201502,MIE03E201902

**Fault Symptom**

Coordinator parameter validation failed, and business execution failed.

**Fault Cause**

Setting the TLS context during initialization failed, or reading the token computation result from the Server failed.

**Fault Location**

Please submit an ISSUE.

## MIE03E20070D

**Fault Symptom**

The Coordinator operation was interrupted, and request execution failed.

**Fault Cause**

The asynchronous processing has not finished, and DoWriteFinishRes and DoWriteChunk arrive at the same time.

**Fault Location**

Try to execute the request again, or restart the service.

## MIE03E200A0B,MIE03E200D0B,MIE03E200F0B,MIE03E20110B,MIE03E20120B,MIE03E20130B,MIE03E20190B,MIE03E201A0B

**Fault Symptom**

The current Coordinator configuration or resource is unavailable, and request execution failed.

**Fault Cause**

The current configuration is unavailable, the node connection failed, the current node is unavailable, or no node is available.

**Fault Location**

Please submit an ISSUE.

## MIE03E201008

**Fault Symptom**

Coordinator communication timed out, and business execution failed.

**Fault Cause**

Request scheduling, inference, first token, or tokenizer timed out.

**Fault Location**

Attempt to retry the request or restart the service.

## MIE03E300001,MIE03E300101,MIE03E300301,MIE03E300401,MIE03E300501,MIE03E300601,MIE03E300701

**Fault Symptom**

The Deployer input configuration is invalid, and the Deployer failed to start.

**Fault Cause**

Some configuration items in the configuration file input by the Deployer do not exist or are invalid.

**Fault Location**

Check the configuration items in `user_config.json` and the corresponding configuration files based on the errors in the log.

## MIE03E30000B,MIE03E30040B

**Fault Symptom**

The current Deployer type is not supported, and Deployer execution failed.

**Fault Cause**

The current hardware type or deployment parameter setting is not supported.

**Fault Location**

Modify the reported `user_config.json`/`env.json` configuration items as required by [Full Parameter Description of the user_config.json Configuration File](../configuration/config_reference.md).

## MIE03E300006,MIE03E300306,MIE03E300406

**Fault Symptom**

Deployer resources are limited, and Deployer request execution failed.

**Fault Cause**

The number of instances exceeds the upper limit.

**Fault Location**

Check whether the configuration in the passed `user_config.json` is correct, and modify it.

## MIE03E300009

**Fault Symptom**

Deployer communication failed, and Deployer request execution failed.

**Fault Cause**

Sending the k8s request failed.

**Fault Location**

Check the passed `user_config.json` configuration, and attempt to retry the request or restart the service.

## MIE03E30000A,MIE03E30030A,MIE03E30040A,MIE03E30060A,MIE03E30070A

**Fault Symptom**

Internal logic failed in the Deployer, and Deployer business execution failed.

**Fault Cause**

This is a fault log triggered by an internal logic failure. For the specific fault cause, see the lower-layer logs nearby.

**Fault Location**

Please submit an issue.

## MIE03E300002

**Fault Symptom**

Deployer parameter validation failed, and Deployer business execution failed.

**Fault Cause**

Validation of the k8s response parameters failed.

**Fault Location**

Please submit an ISSUE.

## MIE03E300005,MIE03E300205,MIE03E300305,MIE03E300505,MIE03E300605,MIE03E300705

**Fault Symptom**

The configuration file required by the Deployer does not exist, and the Deployer failed to start.

**Fault Cause**

The configuration file is missing when the Deployer starts execution.

**Fault Location**

According to [deploy.py Usage](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/README.md#deploypy-usage) and [Configuration File Description](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/README.md#configuration-file-description), check whether the configuration file path is correctly specified.

## MIE03E300007

**Fault Symptom**

The Deployer failed to apply for resources, and the Deployer failed to start.

**Fault Cause**

Failed to create a thread instance.

**Fault Location**

Check whether memory resources are exhausted.

## MIE03E300204,MIE03E300304,MIE03E300404

**Fault Symptom**

A Deployer resource is operated on repeatedly, and the Deployer request fails to execute.

**Fault Cause**

The inference service creator/inference service is inserted/created repeatedly.

**Fault Location**

Internal resource duplicate registration. Submit an ISSUE.

## MIE03E300400

**Fault Symptom**

The Deployer caught an exception and reported an error, and Deployer business execution failed.

**Fault Cause**

A string processing exception occurred during configuration initialization.

**Fault Location**

Check the exception cause and modify the configuration file.
