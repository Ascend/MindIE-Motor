# 版本配套说明

## 产品版本信息

| 项目   | 内容             |
| ---- | -------------- |
| 产品名称 | MindIE PyMotor |
| 产品版本 | 3.0.0          |
| 版本类型 | 正式版本           |
| 维护周期 | 三个月            |

## 相关产品版本配套说明

| 产品名称                         | 版本                                                                                                                                                            |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CANN                         | 8.5.1                                                                                                                                                         |
| vllm                         | v0.18.0                                                                                                                                                       |
| vLLM Ascend                  | releases/v0.18.0                                                                                                                                              |
| Ascend Extension for PyTorch | 7.3.0                                                                                                                                                         |
| Mooncake                     | v0.3.9                                                                                                                                                        |
| Ascend HDK                   | 版本配套关系参见 [CANN版本配套说明](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850/releasenote/releasenote_0000.html) （注：CANN8.5.1和8.5.0版本配套的HDK版本一致） |

# 版本兼容性说明

MindIE各组件需要配套使用，请勿跨版本混用各组件。

**表 1**  软件版本兼容性说明

| MindIE PyMotor | CANN  | vllm    | vllm Ascend      | MindCluster | pytorch | Ascend Extension for PyTorch | CCAE                           |
| -------------- | ----- | ------- | ---------------- | ----------- | ------- | ---------------------------- | ------------------------------ |
| 3.0.0          | 8.5.1 | v0.18.0 | releases/v0.18.0 | 26.0        | 2.9.0   | 7.3.0                        | iMaster CCAE V100R026C00SPC010 |

# 版本使用注意事项

无

# 3.0.0更新说明

## 新增特性

- MindIE PyMotor PD分离和大规模专家并行部署
- Controller Coordinator主备份切换
- PD实例重调度
- 对接vLLM推理引擎

## 修改特性

无

## 删除特性

无

## 接口变更说明

无

## 已解决的问题

无

## 遗留问题

无

# 升级影响

## 升级过程对现行系统的影响

- 对业务的影响

  软件版本升级过程中会导致业务中断。

- 对网络通信的影响

  对网络通信无影响。

## 升级后对现行系统的影响

- 对业务的影响

  对业务无影响。

- 对网络通信的影响

  对网络通信无影响。

# 漏洞修补列表

| 软件名称         | 软件版本                                                                                                                                               | CVE编号          | 实际CVSS得分 | 漏洞描述                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 解决版本         |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| Transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14921 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must visit a malicious page or open a malicious file.The specific flaw exists within the parsing of model files. The issue results from the lack of proper validation of user-supplied data, which can result in deserialization of untrusted data. An attacker can leverage this vulnerability to execute code in the context of the current user.                                                                                                   | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14924 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must visit a malicious page or open a malicious file.The specific flaw exists within the parsing of checkpoints. The issue results from the lack of proper validation of user-supplied data, which can result in deserialization of untrusted data. An attacker can leverage this vulnerability to execute code in the context of the current process.                                                                                                | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14930 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must visit a malicious page or open a malicious file.The specific flaw exists within the parsing of weights. The issue results from the lack of proper validation of user-supplied data, which can result in deserialization of untrusted data. An attacker can leverage this vulnerability to execute code in the context of the current process.                                                                                                    | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14920 | 0        | A vulnerability, which was classified as critical, has been found in Hugging Face transformers (affected version not known).Using CWE to declare the problem leads to CWE-502. The product deserializes untrusted data without sufficiently verifying that the resulting data will be valid.Impacted is confidentiality, integrity, and availability.There is no information about possible countermeasures known. It may be suggested to replace the affected object with an alternative product.                                                                                                                                                               | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14929 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must visit a malicious page or open a malicious file.The specific flaw exists within the parsing of checkpoints. The issue results from the lack of proper validation of user-supplied data, which can result in deserialization of untrusted data. An attacker can leverage this vulnerability to execute code in the context of the current process.                                                                                                | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14926 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must convert a malicious checkpoint.The specific flaw exists within the convert_config function. The issue results from the lack of proper validation of a user-supplied string before using it to execute Python code. An attacker can leverage this vulnerability to execute code in the context of the current user.                                                                                                                               | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14927 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must convert a malicious checkpoint.The specific flaw exists within the convert_config function. The issue results from the lack of proper validation of a user-supplied string before using it to execute Python code. An attacker can leverage this vulnerability to execute code in the context of the current user.                                                                                                                               | MindIE 3.0.0 |
| transformers | 4.30.2,4.33.0,4.33.1,4.34.1,4.35.0,4.36.0,4.36.2,4.37.0,4.37.1,4.37.2,4.38.2,4.39.0,4.40.0,4.42.0,4.42.4,4.43.1,4.43.2,4.44.0,4.46.2,4.49.0,4.51.0 | CVE-2025-14928 | 0        | This vulnerability allows remote attackers to execute arbitrary code on affected installations of Hugging Face Transformers. User interaction is required to exploit this vulnerability in that the target must convert a malicious checkpoint.The specific flaw exists within the convert_config function. The issue results from the lack of proper validation of a user-supplied string before using it to execute Python code. An attacker can leverage this vulnerability to execute code in the context of the current user.                                                                                                                               | MindIE 3.0.0 |
| jinja2       | 3.1.3,3.1.4                                                                                                                                        | CVE-2024-56201 | 5.4      | Jinja is an extensible templating engine. In versions on the 3.x branch prior to 3.1.5, a bug in the Jinja compiler allows an attacker that controls both the content and filename of a template to execute arbitrary Python code, regardless of if Jinja_x27;s sandbox is used. To exploit the vulnerability, an attacker needs to control both the filename and the contents of a template. Whether that is the case depends on the type of application using Jinja. This vulnerability impacts users of applications which execute untrusted templates where the template author can also choose the template filename. This vulnerability is fixed in 3.1.5. | MindIE 3.0.0 |

注：实际CVSS得分为0，即产品无实际漏洞攻击场景，不受漏洞影响（代码未编译、代码无调用、编译选项保护等）。
