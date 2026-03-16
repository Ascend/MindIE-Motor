/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include "Coordinator.h"
#include <cstdlib>
#include "CoordinatorLeaderAgent.h"
#include "DistributedPolicy.h"
#include "Logger.h"
#include "Configure.h"
#include "Configure.h"
#include "SchedulerFactory.h"
#include "IPCConfig.h"
#include "HeartbeatProducer.h"
#include "MemoryUtil.h"
#include "msServiceProfiler/Tracer.h"

using namespace MINDIE::MS;
namespace {
constexpr auto TOKENIZER_WEIGHT_PATH_ENV = "DIGS_tokenizer_weight_path";
constexpr uint32_t MANAGER_THREAD_NUM = 128;
constexpr uint32_t EXTERNAL_THREAD_NUM = 128;     // 外部指标服务器线程数
constexpr uint32_t STATUS_THREAD_NUM = 128;
}

Coordinator::Coordinator() {}

Coordinator::~Coordinator()
{
    if (m_heartbeatProducer) {
        m_heartbeatProducer->Stop();
        LOG_I("[Coordinator] Heartbeat producer stopped.");
    }

    dataHttpServer.Stop();
    managerHttpServer.Stop();
    if (managerThread != nullptr) {
        if (managerThread->joinable()) {
            managerThread->join();
        }
    }
}

HttpServerParm Coordinator::InitExternalListener()
{
    metricListener = std::make_unique<MetricListener>(
        instancesRecord, Configure::Singleton()->promMetricsConfig.reuseTime);
    ServerHandler externalHandler;
    externalHandler.RegisterFun(boost::beast::http::verb::get, "/metrics",
        std::bind(&MetricListener::PrometheusMetricsHandler, metricListener.get(), std::placeholders::_1));
    HttpServerParm parm3;
    parm3.address = Configure::Singleton()->httpConfig.managementIp;
    parm3.port = Configure::Singleton()->httpConfig.externalPort;
    parm3.serverHandler = externalHandler;
    parm3.tlsItems = Configure::Singleton()->externalTlsItems;
    parm3.maxKeepAliveReqs = Configure::Singleton()->reqLimit.connMaxReqs;
    return parm3;
}

HttpServerParm Coordinator::InitStatusListener(std::unique_ptr<ControllerListener>& statusListener)
{
    ServerHandler statusHandler;
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v1/startup",
        std::bind(&ControllerListener::StartUpProbeHandler, statusListener.get(), std::placeholders::_1));
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v1/health",
        std::bind(&ControllerListener::HealthProbeHandler, statusListener.get(), std::placeholders::_1));
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v1/readiness",
        std::bind(&ControllerListener::CoordinatorReadinessProbeHandler, statusListener.get(), std::placeholders::_1));
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v2/health/live",
        std::bind(&ControllerListener::HealthProbeHandler, statusListener.get(), std::placeholders::_1));
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v2/health/ready",
        std::bind(&ControllerListener::ReadinessProbeHandler, statusListener.get(), std::placeholders::_1));
    statusHandler.RegisterFun(boost::beast::http::verb::get, "/v2/models/*/ready",
        std::bind(&ControllerListener::TritonModelsReadyHandler, statusListener.get(), std::placeholders::_1));
    HttpServerParm parm4;
    parm4.address = Configure::Singleton()->httpConfig.managementIp;
    parm4.port = Configure::Singleton()->httpConfig.statusPort;
    parm4.serverHandler = statusHandler;
    parm4.tlsItems = Configure::Singleton()->statusTlsItems;
    parm4.maxKeepAliveReqs = Configure::Singleton()->reqLimit.connMaxReqs;
    return parm4;
}

// wait and listen msg from controller
HttpServerParm Coordinator::InitControllerListener(std::unique_ptr<ControllerListener>& controlListener)
{
    ServerHandler managerHandler;
    managerHandler.RegisterFun(boost::beast::http::verb::post, "/v1/instances/refresh",
        std::bind(&ControllerListener::InstancesRefreshHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::get, "/v1/coordinator_info",
        std::bind(&ControllerListener::CoordinatorInfoHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::post, "/v1/instances/offline",
        std::bind(&ControllerListener::InstancesOfflineHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::post, "/v1/instances/online",
        std::bind(&ControllerListener::InstancesOnlineHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::get, "/v1/instances/tasks*",
        std::bind(&ControllerListener::InstancesTasksHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::post, "/v1/instances/query_tasks",
        std::bind(&ControllerListener::InstancesQueryTasksHandler, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::get, "/recvs_info",
        std::bind(&ControllerListener::RecvsUpdater, controlListener.get(), std::placeholders::_1));
    managerHandler.RegisterFun(boost::beast::http::verb::post, "/backup_info",
        std::bind(&ControllerListener::AbnormalStatusHandler, controlListener.get(), std::placeholders::_1));
    HttpServerParm parm2;
    parm2.address = Configure::Singleton()->httpConfig.managementIp;
    parm2.port = Configure::Singleton()->httpConfig.managementPort;
    parm2.serverHandler = managerHandler;
    parm2.tlsItems = Configure::Singleton()->controllerServerTlsItems;
    parm2.maxKeepAliveReqs = Configure::Singleton()->reqLimit.connMaxReqs;
    return parm2;
}

// wait and listen msg from requestRepeater
HttpServerParm Coordinator::InitRequestListener()
{
    requestListener = std::make_unique<RequestListener>(scheduler, reqManager, instancesRecord, exceptionMonitor,
        requestRepeater);
    // register msg process function while rcv one request from certain url
    ServerHandler serverHandler;
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v2/models/*/generate_stream",
        std::bind(&RequestListener::TritonReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v2/models/*/infer",
        std::bind(&RequestListener::TritonReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v2/models/*/generate",
        std::bind(&RequestListener::TritonReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/",
        std::bind(&RequestListener::TGIStreamReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/generate_stream",
        std::bind(&RequestListener::TGIStreamReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/generate",
        std::bind(&RequestListener::TGIOrVLLMReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v1/completions",
        std::bind(&RequestListener::OpenAIReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v1/chat/completions",
        std::bind(&RequestListener::OpenAIReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/infer",
        std::bind(&RequestListener::MindIEReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/infer_token",
        std::bind(&RequestListener::MindIEReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(boost::beast::http::verb::post, "/v1/tokenizer",
        std::bind(&RequestListener::TokenizerReqHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(ServerHandlerType::RES_CHUNK,
        std::bind(&RequestListener::ServerResChunkHandler, requestListener.get(), std::placeholders::_1));
    serverHandler.RegisterFun(ServerHandlerType::EXCEPTION_CLOSE,
        std::bind(&RequestListener::ServerExceptionCloseHandler, requestListener.get(), std::placeholders::_1));

    HttpServerParm parm1;
    parm1.address = Configure::Singleton()->httpConfig.predIp;
    parm1.port = Configure::Singleton()->httpConfig.predPort;
    parm1.serverHandler = serverHandler;
    parm1.tlsItems = Configure::Singleton()->requestServerTlsItems;
    parm1.maxKeepAliveReqs = Configure::Singleton()->reqLimit.connMaxReqs;
    parm1.keepAliveS = Configure::Singleton()->httpConfig.keepAliveS;
    return parm1;
}

// init scheduler to pick execute node for request
int32_t Coordinator::InitScheduler()
{
    std::string schedulerType = Configure::Singleton()->schedulerConfig["scheduler_type"];
    LOG_I("[Coordinator] scheduler type: %s.", schedulerType.c_str());
    // 根据调度器类型，创建调度器
    scheduler = MINDIE::MS::SchedulerFactory::GetInstance().CreateScheduler(schedulerType,
        Configure::Singleton()->schedulerConfig);
    if (scheduler == nullptr) {
        LOG_E("[%s] [Coordinator] Invalid scheduler type: %s.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::COORDINATOR).c_str(),
            schedulerType.c_str());
        return -1;
    }

    // 根据部署模式（pd分离或者单机部署），注册对应的转发器的钩子函数
    std::string deployMode = Configure::Singleton()->schedulerConfig["deploy_mode"]; // 部署模式
    LOG_I("[Coordinator] Current deploy mode is %s.", deployMode.c_str());
    if (deployMode == "single_node") {
        scheduler->RegisterSingleNodeNotifyAllocation(std::bind(&RequestRepeater::SingleNodeHandler,
            requestRepeater.get(), std::placeholders::_1, std::placeholders::_2));
    }

    if (deployMode == "pd_separate" || deployMode == "pd_disaggregation" ||
        deployMode == "pd_disaggregation_single_container") {
        scheduler->RegisterPDNotifyAllocation(std::bind(&RequestRepeater::PDRouteHandler,
            requestRepeater.get(), std::placeholders::_1, std::placeholders::_2,
            std::placeholders::_3));
    }
    // 启动调度器
    scheduler->Start();

    return 0;
}

// init repeater to send request and rcv result from mindie server
int32_t Coordinator::InitRepeater()
{
    try {
        requestRepeater = std::make_unique<RequestRepeater>(scheduler, reqManager, instancesRecord, exceptionMonitor);
    } catch (const std::exception &e) {
        LOG_E("[%s] [Coordinator] Initialize request repeater failed.",
              GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::COORDINATOR).c_str());
        return -1;
    }
    auto ret = requestRepeater->Init();
    if (ret != 0) {
        LOG_E("[%s] [Coordinator] Initialize request repeater failed.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::COORDINATOR).c_str());
        return ret;
    }
    return 0;
}

void Coordinator::InitGlobleInfoStore()
{
    try {
        instancesRecord = std::make_unique<ClusterNodes>();
        reqManager = std::make_unique<ReqManage>(scheduler, perfMonitor, instancesRecord);
        perfMonitor = std::make_unique<PerfMonitor>();
        perfMonitor->Start();
        timeoutMonitor = std::make_unique<RequestMonitor>(reqManager, exceptionMonitor);
        timeoutMonitor->Start();
    } catch (const std::exception &e) {
        LOG_E("[%s] [Coordinator] init globleInfo store failed, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
        return;
    }
}

void Coordinator::InitExceptionMonitor()
{
    exceptionMonitor = std::make_unique<ExceptionMonitor>();
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::RETRY, std::bind(&RequestListener::ReqRetryHandler,
        requestListener.get(), std::placeholders::_1));
    exceptionMonitor->RegInsExceptionFun(InsExceptionType::CONN_P_ERR, std::bind(
        &RequestRepeater::ConnPErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::SEND_P_ERR, std::bind(
        &RequestRepeater::ReqSendPErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegInsExceptionFun(InsExceptionType::CONN_D_ERR, std::bind(&RequestRepeater::ConnDErrHandler,
        requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegInsExceptionFun(InsExceptionType::CONN_MIX_ERR, std::bind(
        &RequestRepeater::ConnMixErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::SEND_MIX_ERR, std::bind(
        &RequestRepeater::ReqSendMixErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::USER_DIS_CONN, std::bind(
        &RequestRepeater::UserDisConnHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::DECODE_DIS_CONN, std::bind(
        &RequestRepeater::DecodeDisConnHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::SCHEDULE_TIMEOUT, std::bind(
        &RequestRepeater::ReqScheduleTimeoutHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::FIRST_TOKEN_TIMEOUT, std::bind(
        &RequestRepeater::ReqFirstTokenTimeoutHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::INFER_TIMEOUT, std::bind(
        &RequestRepeater::ReqInferTimeoutHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegInsExceptionFun(InsExceptionType::CONN_TOKEN_ERR, std::bind(
        &RequestRepeater::TokenizerConnErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::SEND_TOKEN_ERR, std::bind(
        &RequestRepeater::TokenizerSendErrHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::TOKENIZER_TIMEOUT, std::bind(
        &RequestRepeater::ReqTokenizerTimeoutHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->RegReqExceptionFun(ReqExceptionType::RETRY_DUPLICATE_REQID, std::bind(
        &RequestRepeater::RetryDuplicateReqIdHandler, requestRepeater.get(), std::placeholders::_1));
    exceptionMonitor->Start();
}

// 必须在controllerListener、requestListener初始化完成之后执行
void Coordinator::InitLeader()
{
    LOG_I("[Coordinator] InitLeader.");
    // 开启主备
    if (Configure::Singleton()->CheckBackup()) {
        auto backCfg = Configure::Singleton()->coordinatorBackUpConfig;
        const std::string partAddr = backCfg.serverDns;
        const std::string etcdAddr = partAddr + ":" + std::to_string(backCfg.serverPort);

        const char* podIpEnv = std::getenv("POD_IP");
        std::string podIp = (podIpEnv != nullptr) ? podIpEnv : Configure::Singleton()->httpConfig.predIp;
        auto tlsConfig = Configure::Singleton()->coordinatorEtcdTlsItems;
        EtcdTimeInfo etcdTimeInfo;
        auto etcd_lock = std::make_unique<EtcdDistributedLock>(
            etcdAddr,
            "/coordinator/leader-lock", // use for lock key
            podIp, // use for lock value
            tlsConfig, // use for etcd tls
            etcdTimeInfo // use for etcd time info
        );
        CoordinatorLeaderAgent::GetInstance()->SetListener(controllerListener.get(), requestListener.get());
        CoordinatorLeaderAgent::GetInstance()->RegisterStrategy(std::move(etcd_lock));
        CoordinatorLeaderAgent::GetInstance()->Start();
        LOG_I("[LeaderAgent] leader Campaign finish, serve ip is %s, is leader: %d.",
              etcdAddr.c_str(), Configure::Singleton()->IsMaster());
    } else {
        Configure::Singleton()->SetMaster(true);
        // Don't need to create link because link is created at AddInsWithoutBackup
        LOG_D("[Coordinator] I'm master.");
    }
}

int32_t Coordinator::StartManagerServer()
{
    try {
        controllerListener = std::make_unique<ControllerListener>(scheduler, instancesRecord, requestRepeater,
            reqManager, exceptionMonitor);
        HttpServerParm controllerParam = InitControllerListener(controllerListener);
        HttpServerParm statusParam = InitStatusListener(controllerListener);
        HttpServerParm externalParam = InitExternalListener();
        managerThread = std::make_unique<std::thread>([controllerParam, this] {
            try {
                LOG_M("[Start] MindIE-MS coordinator start manager server.");
                // 线程池固定，Init接口不会返回-1
                (void)(this->managerHttpServer.Init(MANAGER_THREAD_NUM));
                this->managerHttpServer.Run({controllerParam});
            } catch (const std::exception& e) {
                LOG_E("[%s] [Coordinator] Coordinator manager server exit, error is %s.",
                    GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
            }
        });
        // 启动外部指标服务器
        externalThread = std::make_unique<std::thread>([externalParam, this] {
            try {
                LOG_M("[Start] MindIE-MS coordinator start external metrics server.");
                (void)(this->externalHttpServer.Init(EXTERNAL_THREAD_NUM)); // 需要定义EXTERNAL_THREAD_NUM
                this->externalHttpServer.Run({externalParam});
            } catch (const std::exception& e) {
                LOG_E("[%s] [Coordinator] Coordinator external server exit, error is %s.",
                    GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
            }
        });
        // 启动状态检查服务器
        statusThread = std::make_unique<std::thread>([statusParam, this] {
            try {
                LOG_M("[Start] MindIE-MS coordinator start status server.");
                (void)(this->statusHttpServer.Init(STATUS_THREAD_NUM)); // 需要定义STATUS_THREAD_NUM
                this->statusHttpServer.Run({statusParam});
            } catch (const std::exception& e) {
                LOG_E("[%s] [Coordinator] Coordinator status server exit, error is %s.",
                    GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
            }
        });
    } catch (const std::exception& e) {
        LOG_E("[%s] [Coordinator] Start manager server failed, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
        return -1;
    }
    return 0;
}


void TraceInit()
{
    try {
        if (!msServiceProfiler::Tracer::IsEnable()) {
            return;
        }
        msServiceProfiler::TraceContext::addResAttribute("service.name", "mindie.motor");
    } catch (const std::exception& e) {
        LOG_W("Trace Module maybe init failed, ignore and continue, warning is %s", e.what());
    }
}


int32_t Coordinator::Run()
{
    try {
        auto producerInterval = std::chrono::milliseconds(HEARTBEAT_PRODUCER_INTERVAL_MS);
        // Using Coordinator specific heartbeat SHM/SEM names
        m_heartbeatProducer = std::make_unique<HeartbeatProducer>(
            std::chrono::milliseconds(producerInterval),
            HB_COORD_SHM_NAME, HB_COORD_SEM_NAME, DEFAULT_HB_BUFFER_SIZE
        );
        m_heartbeatProducer->Start();
        LOG_I("[Coordinator] Heartbeat producer started successfully for SHM: %s, SEM: %s.",
            HB_COORD_SHM_NAME, HB_COORD_SEM_NAME);
    } catch (const std::exception& e) {
        LOG_E("[Coordinator] Failed to initialize or start heartbeat producer: %s", e.what());
        return -1;
    }

    InitGlobleInfoStore();
    if (InitRepeater() != 0 || InitScheduler() != 0) {
        LOG_E("[%s] [Coordinator] Coordinator failed to init repeater or scheduler.",
            GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::COORDINATOR).c_str());
        return -1;
    }

    HttpServerParm param2;
    try {
        param2 = InitRequestListener();
        InitExceptionMonitor();
        auto ret = StartManagerServer();
        if (ret != 0) {
            LOG_E("[%s] [Coordinator] Coordinator start manager server failed.",
                GetErrorCode(ErrorType::CALL_ERROR, CoordinatorFeature::COORDINATOR).c_str());
            return ret;
        }
    } catch (const std::exception &e) {
        LOG_E("[%s] [Coordinator] failed init request listener or exception monitor, error is %s.",
              GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
        return -1;
    }
    InitLeader();
    // check request config memory risk
    MemoryUtil::CheckRequestConfigMemoryRisk();
    LOG_M("[Start] MindIE-MS coordinator start successful.");
    LOG_I("MindIE-MS coordinator is not ready...");
    TraceInit();
    // start to work and ready to rcv request from controller and requestRepeater
    try {
        LOG_M("[Start] MindIE-MS coordinator start data server.");
        size_t maxConn = Configure::Singleton()->reqLimit.maxReqs; // Maximum connections equal maximum requests.
        // serverThreadNum在配置校验时已约束不会等于0，因此Init接口不会返回-1
        (void)(dataHttpServer.Init(Configure::Singleton()->httpConfig.serverThreadNum, maxConn));
        // http server will block and waiting for request coming
        dataHttpServer.Run({param2}, controllerListener->GetDataReady());
    } catch (const std::exception& e) {
        LOG_E("[%s] [Coordinator] Coordinator data server exit, error is %s.",
            GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::COORDINATOR).c_str(), e.what());
        return -1;
    }
    return 0;
}