# 测试的编译&运行

## llt测试

1. 先执行MindIE_MS的one_key_build.sh。

    ```bash
    bash one_key_build.sh
    ```

2. 再构建测试代码。

    ```bash
    bash test_mindms.sh
    ```

3. 在llt目录下创建log日志。

    ```bash
    mkdir log
    ```

4. 在llt下运行mindie_ms_llt。

    ```bash
    ./mindie_ms_llt
    ```
