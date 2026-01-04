## 场景介绍

### PD分离服务部署介绍

Prefill&Decode分离服务部署（简称：PD分离服务部署），PD分离服务部署又分为单机PD分离服务部署和多机PD分离服务部署，是将Prefill（预填充）和Decode（解码）这两个推理阶段分开处理的技术，通常适用于对时延有严格要求的场景。PD分离服务部署可以提高NPU的利用率，尤其是大语言模型，将Prefill实例和Decode实例分开部署，减少Prefill阶段和Decode阶段分时复用在时延上造成的互相干扰，实现同时延下吞吐提升。PD分离工作原理如图1所示。

目前支持PD分离单机和PD分离多机服务部署：

- PD分离单机服务部署：Controller、Coordinator和Server组件全部运行在同一个Pod内，适用于部署在一台服务器的场景。
- PD分离多机服务部署：Controller、Coordinator和Server组件分别运行在独立的Pod内，适用于部署在多台服务器的场景。

**图1** PD分离工作原理

![img](https://raw.gitcode.com/Ascend/MindIE-Motor/raw/br_develop_mindie/docs%2Fzh%2Ffigures%2Fworking_principle_pd.png)

大语言模型推理的阶段可以分为Prefill与Decode阶段：

- Prefill阶段：在生成式语言模型中，Prefill阶段涉及到模型对初始提示（Prompt）的处理，生成初始的隐藏状态（Hidden States）。这个阶段通常涉及对整个模型的一次前向传播，因此计算密集度较高。对于每个新的输入序列，都需要进行一次Prefill。
- Decode阶段：在Prefill阶段之后，模型基于初始隐藏状态逐步生成后续的文本。这一阶段的特点是计算相对较少，但需要反复进行计算，直到生成足够的文本或达到某个终止条件。在生成过程中，只计算最新的token激活值，并进行attention计算，计算最终的预测token。

### 部署方案

- 单机PD分离服务部署方案：

  通过K8s的Service开放PD集群的推理入口，创建1个K8s的Deployment部署一个Pod，其中以进程方式分别部署Controller（单进程副本）、Coordinator（单进程副本）以及Server（多进程副本）。

  **图2** 单机PD分离服务部署方案

  ![img](https://raw.gitcode.com/Ascend/MindIE-Motor/raw/br_develop_mindie/docs%2Fzh%2Ffigures%2Fsingle_pd_deploymet_plan.png)

- 多机PD分离服务部署方案：

  通过K8s的Service为Coordinator Pod开放PD集群的推理入口，创建3个K8s的Deployment分别部署Controller（单Pod副本）、Coordinator（单Pod副本）以及Server（多Pod副本）。

  **图3** 多机PD分离服务部署方案

  ![img](https://raw.gitcode.com/Ascend/MindIE-Motor/raw/br_develop_mindie/docs%2Fzh%2Ffigures%2Fmuilt_pd_deploymet_plan.png)

 

### PD分离优势

PD分离主要包括以下优势：

- 资源利用优化：由于Prefill阶段计算密集，而Decode阶段计算较为稀疏，将这两个阶段分离可以更好的利用NPU的计算资源。
- 提高吞吐量：分离后的Prefill和Decode可以同时处理不同的请求，这意味着在Prefill阶段处理新请求的同时，Decode阶段可以继续处理之前请求的解码任务，从而提高了整体的处理能力。
- 降低延迟：由于Prefill和Decode分别在不同的阶段进行，可以减少等待时间，特别是当有多个请求并发到达时。

#### 限制与约束

- 单机PD分离服务部署
  - 仅Atlas 800I A2 推理服务器和Atlas 800I A3 超节点服务器支持此特性。
  - 不同P、D节点使用的NPU卡数量必须相同。
  - 仅支持LLaMA3系列和Qwen2系列。
  - 支持与Prefix Cache特性同时使用。
  - 不支持和稀疏量化、KV Cache int8量化配合使用。
  - 该特性暂不支持n、best_of、use_beam_search、logprobs、top_logprobs等与多序列推理相关的后处理参数。
- 多机PD分离服务部署
  - 仅Atlas 800I A2 推理服务器支持此特性。
  - P节点与D节点仅支持相同型号的机型。
  - P节点与D节点使用的NPU卡数量必须相同。
  - NPU网口互联（带宽：200Gbps）。
  - 不支持和Multi LoRA、并行解码、SplitFuse、Prefix Cache、Function Call、多机推理和长序列特性同时使用。
  - 仅支持LLaMA3系列和Qwen2系列。
  - 不支持和稀疏量化、KV Cache int8量化配合使用。
  - 该特性暂不支持n、best_of、use_beam_search、logprobs、top_logprobs等与多序列推理相关的后处理参数。
- 当前不支持“停止字符串”方式停止推理文本（即不支持stop，include_stop_str_in_output参数，参数详情请参见[推理接口](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/SERVICE_ORIENTED_INTERFACE/optical_user_to_network_interface.md)）。

### 硬件环境

PD分离部署支持的硬件环境如表1所示。

表1 PD分离部署支持的硬件列表

| 类型   | 型号                       | 内存     |
| ------ | -------------------------- | -------- |
| 服务器 | Atlas 800I A2 推理服务器   | 32GB64GB |
| 服务器 | Atlas 800I A3 超节点服务器 | 64GB     |

说明

- 集群必须具备参数面互联：即服务器NPU卡对应的端口处在同一个VLAN，可以通过RoCE互通。
- 为保障业务稳定运行，用户应严格控制自建Pod的权限，避免高权限Pod修改MindIE内部参数而导致异常。

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
    <li>集群必须具备参数面互联：即服务器NPU卡对应的端口处在同一个VLAN，可以通过RoCE互通。</li>
    <li>为保障业务稳定运行，用户应严格控制自建Pod的权限，避免高权限Pod修改MindIE内部参数而导致异常。</li>
</ul></div>
## 配置自动生成证书

若用户开启Server的TLS认证功能（HTTPS或gRPC）时，通信客户端需要校验服务端证书的IP，由于PodIP的动态性，需要在Pod启动时生成具有PodIP别名的服务证书，以实现Server中PD节点间的通信，以及集群管理组件对Server的证书认证和校验。MindIE提供证书生成能力，具体操作步骤如下所示。

建议用户在运行环境中的各个计算节点准备和配置证书，提升服务安全性。

### 操作步骤

此方法只适用于使用自签名CA证书进行证书签发的场景。

需要按照以下方法准备Server、Controller和Coordinator三套证书。

1. 准备自签名CA证书和加密私钥及导入。

   a. 执行以下命令生成配置文件。

      ```
   cat > ca.conf <<-EOF
      ```

      配置文件ca.conf示例如下，其中req_distinguished_name中的字段需要自行配置：

      ```
   [ req ]
   distinguished_name    = req_distinguished_name
   prompt                = no
   
   [ req_distinguished_name ]
   C                     = CN
   ST                    = Sichuan
   L                     = Chengdu
   O                     = Huawei
   OU                    = Ascend
   CN                    = MindIE
   
   [ v3_ca ]
   subjectKeyIdentifier = hash
   authorityKeyIdentifier = keyid:always,issuer
   basicConstraints = critical, CA:true
   keyUsage = critical, digitalSignature, cRLSign, keyCertSign
   EOF
      ```

   b. 执行以下命令创建格式为PKCS#1的PKI私钥ca.key.pem。

      ```
   openssl genrsa -aes256 -out ca.key.pem 4096
      ```

   c. 根据回显输入私钥口令，然后按回车键。

      ```
   Enter pass phrase for ca.key.pem:
   Verifying - Enter pass phrase for ca.key.pem:
      ```

      出于安全考虑，以及后续导入证书的要求，用户输入的私钥口令的复杂度必须符合以下要求：

      - 口令长度至少8个字符；
      - 口令必须包含如下至少两种字符的组合：
        - 至少一个小写字母；
        - 至少一个大写字母；
        - 至少一个数字；
        - 至少一个特殊字符。

   d. 执行以下命令赋予ca.key.pem私钥文件可读权限。

      ```
   chmod 400 ca.key.pem
      ```

   e. 执行以下命令检查是否存在ca.key.pem私钥文件，并查看私钥内容。

      ```
   openssl rsa -in ca.key.pem
      ```

      根据回显输入1.c设置的私钥口令，然后按回车键，当打印私钥内容时表示ca.key.pem私钥文件生成成功。

   f. 执行以下命令创建CSR文件，根据回显输入1.c设置的私钥口令，然后按回车键。

      ```
   openssl req -out ca.csr -key ca.key.pem -new -config ca.conf -batch
      ```

   g. 执行以下命令赋予ca.csr文件可读可写权限。

      ```
   chmod 600 ca.csr
      ```

   h. 执行以下命令检查是否存在ca.csr文件，当打印ca.csr文件内容表示ca.csr文件生成成功。

      ```
   openssl req -in ca.csr -noout -text
      ```

   i. 执行以下命令生成CA证书ca.pem。

      ```
   openssl x509 -req -in ca.csr -out ca.pem -sha256 -days 7300 -extfile ca.conf -extensions v3_ca -signkey ca.key.pem
      ```

   j. 执行以下命令检查是否存在ca.pem文件，当有回显内容时表示ca.pem生成成功。

       ```
       openssl x509 -in ca.pem -noout -text
       ```

   k. 执行以下命令赋予ca.pem文件可读权限。

       ```
       chmod 400 ca.pem
       ```

2. 导入自签名CA证书和加密私钥

   a. 使用以下命令进入MindIE Motor安装目录。

      ```
   cd /{MindIE安装目录}/mindie-service/
      ```

   b. 通过MindIE证书管理工具import_cert接口导入CA证书和私钥，输入证书私钥口令、生成KMC加密口令文件和KMC密钥库文件。MindIE证书管理工具详情请参见[config_mindie_server_tls_cert.py](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/service_oriented_optimization_tool.md#config_mindie_server_tls_certpy)。

      ```
   python3 ./scripts/config_mindie_server_tls_cert.py ./security/ca import_cert  {证书文件路径}  {加密私钥文件路径}
      ```

      参数解释：

      - *{证书文件路径}*：为CA证书的源路径。
      - *{加密私钥文件路径}*：为CA私钥的源路径。

      在回显时输入生成CA密钥时设置的口令：

      ```
   Password for private key file: 
   Retype password for private key file: 
      ```

3. 准备生成证书的配置文件。

   - 生成用户证书的配置文件（gen_cert.json）：

     ```
     {
         "ca_cert": "./security/ca/ca.pem",
         "ca_key": "./security/ca/ca.key.pem",
         "ca_key_pwd": "./security/ca/ca_passwd.txt",
         "cert_config": "./cert_info.json",
         "output_path": "./gen_cert_output",
         "kmc_ksf_master": "./tools/pmt/master/ksfa",
         "kmc_ksf_standby": "./tools/pmt/standby/ksfb"
     }
     ```

   - 配置"cert_config"参数中的cert_info.json配置文件的待生成证书信息：

     ```
     {
         "subject": "subject_name",
         "expired_time": 3650,
         "serial_number": 123,
         "req_distinguished_name": {
             "C": "***",
             "ST": "***",
             "L": "***",
             "O": "***",
             "OU": "***",
             "CN": "***"
         },
         "alt_names": {
             "IP": [],
             "DNS": []
         }
     }
     ```
   
4. 在[脚本介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/SERVICE_DEPLOYMENT/pd_separation_service_deployment.md#%E4%BD%BF%E7%94%A8kubectl%E9%83%A8%E7%BD%B2%E5%A4%9A%E6%9C%BApd%E5%88%86%E7%A6%BB%E6%9C%8D%E5%8A%A1%E7%A4%BA%E4%BE%8B)的mindie_server.yaml、mindie_ms_controller.yaml和mindie_ms_coordinator.yaml配置文件中挂载上述自签名CA证书文件和配置文件到容器内/mnt/security目录，并配置为只读权限。

5. 在[脚本介绍](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/SERVICE_DEPLOYMENT/pd_separation_service_deployment.md#%E4%BD%BF%E7%94%A8kubectl%E9%83%A8%E7%BD%B2%E5%A4%9A%E6%9C%BApd%E5%88%86%E7%A6%BB%E6%9C%8D%E5%8A%A1%E7%A4%BA%E4%BE%8B)的容器启动脚本boot.sh中适配添加证书生成命令，以生成Server的证书为例，在“if [ $exit_code -eq 2 ]; then”分支内添加以下生成证书的命令。

   ```
   cp /mnt/security/ca.pem $MIES_INSTALL_PATH/security/ca
   cp /mnt/security/ca.key.pem $MIES_INSTALL_PATH/security/ca
   cp /mnt/security/ca_passwd.txt $MIES_INSTALL_PATH/security/ca
   cp /mnt/security/gen_cert.json $MIES_INSTALL_PATH
   cp /mnt/security/cert_info.json $MIES_INSTALL_PATH
   cp -r /mnt/security/tools $MIES_INSTALL_PATH/
   chmod 500 ./bin/gen_cert
   mkdir gen_cert_output
   python3 ./scripts/config_mindie_server_tls_cert.py  ./  gen_cert ./gen_cert.json  --ip=$MIES_CONTAINER_IP,{host_ip}
   chmod 400 ./gen_cert_output/*
   // 拷贝生成的证书到特定的路径
   cp ./gen_cert_output/cert.pem /home/{用户名称}/Ascend/mindie/latest/mindie-service/security/certs/server.pem
   cp ./gen_cert_output/cert.key.pem /home/{用户名称}/Ascend/mindie/latest/mindie-service/security/keys/server.key.pem
   cp ./gen_cert_output/cert_passwd.txt /home/{用户名称}/Ascend/mindie/latest/mindie-service/security/pass/mindie_server_key_pwd.txt
   rm -rf ./gen_cert_output/*
   // 下面使用其他证书配置(gen_cert_xxx.json, cert_info_xxx.json)重复上述步骤继续导入其他证书
   // cp /mnt/security/gen_cert_xxx.json $MIES_INSTALL_PATH
   // cp /mnt/security/cert_info_xxx.json $MIES_INSTALL_PATH
   // python3 ./scripts/config_mindie_server_tls_cert.py  ./  gen_cert ./gen_cert_xxx.json  --ip=$MIES_CONTAINER_IP,{host_ip}
   ```

   {host_ip}：仅Coordinator需要配置，配置为提供推理API的物理机IP。

说明

- Server、集群管理组件控制器（Controller）和调度器（Coordinator）三套证书准备完成后，请参考《MindIE LLM开发指南》中的“核心概念与配置 >[配置参数说明（服务化）](https://gitcode.com/Ascend/MindIE-LLM/blob/br_develop_mindie/docs/zh/user_guide/user_manual/service_parameter_configuration.md)”章节、[控制器（Controller）配置说明](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/controller.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)和[调度器（Coordinator）配置说明](https://gitcode.com/Ascend/MindIE-Motor/blob/br_develop_mindie/docs/zh/User_Guide/ClLUSTER_MANAGEMENT_COMPONENT/coordinator.md#%E9%85%8D%E7%BD%AE%E8%AF%B4%E6%98%8E)将每个证书拷贝至指定的路径下。
- 启动Server Pod调用生成证书接口如出现“failed to read random number from system.”报错，大概率是由于环境熵不足，需要在计算节点安装haveged组件补熵。详情请参考《MindIE安装指南》中的“附录 > [启动haveged服务](https://www.hiascend.com/document/detail/zh/mindie/22RC1/envdeployment/instg/mindie_instg_0088.html)”章节，将熵补至4096。

<div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
<strong>💡 说明</strong>
<ul style="margin:8px 0;padding-left:20px;">
    <li>Server、集群管理组件控制器（Controller）和调度器（Coordinator）三套证书准备完成后，请参考《MindIE LLM开发指南》中的“核心概念与配置 >配置参数说明（服务化）”章节、控制器（Controller）配置说明和调度器（Coordinator）配置说明将每个证书拷贝至指定的路径下。</li>
    <li>启动Server Pod调用生成证书接口如出现“failed to read random number from system.”报错，大概率是由于环境熵不足，需要在计算节点安装haveged组件补熵。详情请参考《MindIE安装指南》中的“附录 > 启动haveged服务”章节，将熵补至4096。</li>
</ul></div>