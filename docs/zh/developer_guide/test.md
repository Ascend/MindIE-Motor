*服务端测试指南，包含压力测试、并发测试、集成测试、API测试的方法和工具使用*

# MindIE-Motor DT本地使用

## ms DT

1. 下载编译代码仓

   ```
   git clone https://gitee.com/Ascend/MindIE-Motor.git
   cd MindIE-Motor
   bash build/build.sh -d 3rd -b 3rd service
   ```

2. 修改/your_code_path/MindIE-Motor/tests/ms/ms_test_util.sh，搜索j8，将两处均改为j

3. 修改/your_code_path/MindIE-Motor/tests/ms/ms_test_util.sh，将run_smoke_dt_test函数不需要的内容注释掉

4. /your_code_path/MindIE-Motor/tests/ms/dt/CMakeLists.txt，在下面两行代码中间：

   ```
   include_directories
   ```

   ```
   set(HTTP_CLIENT_CTL_DIR ${MindIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)
   ```

   加上下面这行代码：

   ```
   set(OPENSSL_ROOT_DIR ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl)
   ```

5. 执行下面这行代码后，开始运行ms-dt

   ```
   bash build/build.sh -b ms-test
   ```

6. 执行成功后，可在结尾处看到finished提示，且显示DT覆盖率百分数

## Server DT

MindIE Server已经迁移至MindIE-LLM仓

## tools DT

tools主要包含simulator，即将日落

## benchmark DT(日落)

benchmark即将日落

## client DT(日落)

MindIE Client即将日落