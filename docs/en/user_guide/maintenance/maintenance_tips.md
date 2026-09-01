# Configuring the MindIE Motor Service Using a YAML File

## Deploying Multiple Motor Services in the Same Cluster

If multiple sets of PD instances are deployed in the same k8s cluster, there will be correspondingly multiple sets of Coordinator and Controller instances. In this case, you need to configure different ports for different Coordinator instances to avoid port conflicts.

The default port of a Coordinator instance is 31015. If two sets of PD instances are deployed onsite, the ports of the two corresponding Coordinator instances are 31015 and 31016 respectively. The steps for modifying the ports are as follows:

  1. Go to the folder specified by the YAML file and open the corresponding file.

      ```bash
      cd examples/deployer/yaml_template/
      vim infer_service_template.yaml
      ```

  2. Search for the `name: coordinator` keyword to locate the following configuration block, modify the `nodePort` field, and save the file.

      ```yaml
      ...
      - name: coordinator
        replicas: 1
        services:
        - name: mindie-motor-coordinator-infer
          spec:
            ports:
            - nodePort: 31015          # This field needs to be modified and represents the service inference plane port
              port: 1025
              protocol: TCP
              targetPort: 1025
            selector:
              app: mindie-motor-coordinator
            sessionAffinity: None
            type: NodePort
            ...
        - name: mindie-motor-coordinator-obs
          spec:
            ports:
            - nodePort: 31017      # This field needs to be modified and represents the service metric observation port
              port: 1027
              protocol: TCP
              targetPort: 1027
            selector:
              app: mindie-motor-coordinator
            sessionAffinity: None
            type: NodePort
      ```

  3. Search for the `name: controller` keyword to locate the following configuration block, modify the `nodePort` field, and save the file:

      ```yaml
      ...
      - name: controller
        replicas: 1
        services:
        - name: mindie-motor-service
          spec:
            ports:
            - port: 1026
              protocol: TCP
              targetPort: 1026
            selector:
              app: mindie-motor-controller
            sessionAffinity: None
            type: ClusterIP
        - name: mindie-motor-observability
          spec:
            ports:
            - nodePort: 31067         # This field needs to be modified and represents the service management plane port
              port: 1027
              protocol: TCP
              targetPort: 1027
            selector:
              app: mindie-motor-controller
            sessionAffinity: None
            type: NodePort
            ...
      ```

      >[!NOTE]NOTE
      > If `"deploy_mode": "multi_deployment"` is configured in `user_config.json`, the traditional multi-YAML deployment method is used. The content to be modified is the same as above, but the files to be modified are different:
      >
      > - Coordinator port: Modify the `nodePort` field in `coordinator_template.yaml`.
      > - Controller port: Modify the `nodePort` field in `controller_template.yaml`.
      >
      > Example commands are as follows:
      >
      > ```bash
      > cd examples/deployer/yaml_template/
      > vim coordinator_template.yaml
      > vim controller_template.yaml
      > ```

## Deploying Coordinator/Controller/Inference Pods on Fixed Servers

By default, Coordinator, Controller, and inference Pods are randomly allocated among the servers in the k8s cluster. If you want these Pods to be deployed on fixed servers, refer to the following operations.

### PD Disaggregation Scenario

  1. On the master node of the k8s cluster, run the following command to label each server in the cluster.

      ```bash
      kubectl label node {node_name} key=value
      ```

      - `node_name`: Enter the server name, which can be queried using the `kubectl get node` command.

      - `key`: the label name.

      - `value`: the label value.

      For example:

      ```bash
      # The controller is deployed on the node-33-137 server
      kubectl label node node-33-137 mindie_controller=controller
      # The coordinator is deployed on the node-33-138 server
      kubectl label node node-33-138 mindie_coordinator=coordinator
      # In the PD disaggregation scenario, the P instance is deployed on the node-33-201 server
      kubectl label node node-33-201 motor_role=prefill
      # In the PD disaggregation scenario, the D instance is deployed on the node-33-203 server
      kubectl label node node-33-203 motor_role=decode
      ```

  2. Modify the initialization file of the Controller instance.

     Run the `vim infer_service_template.yaml` command, search for `name: controller`, and add two fields in the configuration block shown below (`mindie_controller` and `controller` are respectively the label name and label value created in the first step).

      ```yaml
      ...
      - name: controller
        replicas: 1
        ...
        spec:
          replicas: 1
          selector:
            matchLabels:
              app: mindie-motor-controller
          template:
            metadata:
              labels:
                app: mindie-motor-controller
                deploy-name: mindie-motor-controller
            spec:
              nodeSelector:                    # Newly added
                mindie_controller: controller  # Newly added
              serviceAccountName: mindie-motor-controller
              terminationGracePeriodSeconds: 0
              securityContext:
                fsGroup: 1001
      ...
      ```

  3. Modify the initialization file of the Coordinator instance.

     Run the `vim infer_service_template.yaml` command, search for `name: coordinator`, and add two fields in the configuration block shown below (`mindie_coordinator` and `coordinator` are respectively the label name and label value created in the first step).

      ```yaml
      ...
      - name: coordinator
        replicas: 1
        ...
        spec:
          replicas: 1
          selector:
            matchLabels:
              app: mindie-motor-coordinator
          template:
            metadata:
              labels:
                app: mindie-motor-coordinator
            spec:
              nodeSelector:                      # Newly added
                mindie_coordinator: coordinator  # Newly added
              terminationGracePeriodSeconds: 0
              automountServiceAccountToken: false
              securityContext:
                fsGroup: 1001
      ...
      ```

  4. Modify the initialization file of the inference Pod.

     Run the `vim infer_service_template.yaml` command, search for `name: prefill` and `name: decode` respectively, and append the role label in their respective `template.spec.nodeSelector` (the value of `motor_role` must be consistent with the label name and label value created in the first step):

      ```yaml
      ...
      - name: prefill
        ...
          template:
            spec:
              schedulerName: volcano
              nodeSelector:
                accelerator: huawei-Ascend910
                accelerator-type: module-910b-8
                motor_role: prefill          # Newly added
      ...
      - name: decode
        ...
          template:
            spec:
              schedulerName: volcano
              nodeSelector:
                accelerator: huawei-Ascend910
                accelerator-type: module-910b-8
                motor_role: decode           # Newly added
      ...
      ```

  5. After the modification is complete, redeploy and verify the scheduling result.

      ```bash
      cd examples/deployer
      python deploy.py --config_dir <configuration directory>
      kubectl get pod -n <namespace> -o wide
      ```

      You can observe that each pod is scheduled to different nodes according to the label relationship.

  >[!NOTE]NOTE
  > If `"deploy_mode": "multi_deployment"` is configured in `user_config.json`, the labeling method is the same as above, but the template files to be modified are different:
  >
  > - Controller: Modify `controller_template.yaml` and add `nodeSelector` under `template.spec`.
  > - Coordinator: Modify `coordinator_template.yaml` and add `nodeSelector` under `template.spec`.
  > - Inference Pod (PD disaggregation): Modify `engine_template.yaml` and append `motor_role: prefill` or `motor_role: decode` in `template.spec.nodeSelector`.
  >
  > Example commands are as follows:
  >
  > ```bash
  > cd examples/deployer/yaml_template/
  > vim controller_template.yaml
  > vim coordinator_template.yaml
  > vim engine_template.yaml
  > ```

### PD Co-location Deployment

  1. On the K8s master node, run the following commands to label each server in the cluster.

      ```bash
      kubectl label node {node_name} key=value
      ```

      - `node_name`: Enter the server name, which can be queried using the kubectl get node command.

      - `key`: label name.

      - `value`: label value.

      For example:

      ```bash
      # The controller is deployed on the node-33-137 server
      kubectl label node node-33-137 mindie_controller=controller
      # The coordinator is deployed on the node-33-138 server
      kubectl label node node-33-138 mindie_coordinator=coordinator
      # In the PD hybrid deployment scenario, the co-location inference instance is deployed on the node-33-201 server
      kubectl label node node-33-201 motor_role=hybrid
      ```

  2. Modify the initialization file of the Controller instance.

     Run the `vim controller_template.yaml` command, search for `name: mindie-motor-controller`, and add two fields in the configuration block shown below (`mindie_controller` and `controller` are respectively the label name and label value created in the first step).

      ```yaml
      ...
      template:
        metadata:
          labels:
            app: mindie-motor-controller
            deploy-name: mindie-motor-controller
        spec:
          nodeSelector:                    # Newly added
            mindie_controller: controller  # Newly added
          serviceAccountName: mindie-motor-controller
          terminationGracePeriodSeconds: 0
          securityContext:
            fsGroup: 1001
      ...
      ```

  3. Modify the initialization file of the Coordinator instance.

     Run the `vim coordinator_template.yaml` command, search for `name: mindie-motor-coordinator`, and add two fields in the configuration block shown below (`mindie_coordinator` and `coordinator` are respectively the label name and label value created in the first step).

      ```yaml
      ...
      template:
        metadata:
          labels:
            app: mindie-motor-coordinator
        spec:
          nodeSelector:                      # Newly added
            mindie_coordinator: coordinator  # Newly added
          terminationGracePeriodSeconds: 0
          automountServiceAccountToken: false
          securityContext:
            fsGroup: 1001
      ...
      ```

  4. Modify the initialization file of the inference Pod.

     Run the `vim engine_template.yaml` command, and append the role label in `template.spec.nodeSelector` (the value of `motor_role` must be consistent with the label name and label value created in the first step):

      ```yaml
      ...
      template:
        spec:
          schedulerName: volcano
          nodeSelector:
            accelerator: huawei-Ascend910
            accelerator-type: module-910b-8
            motor_role: hybrid          # Newly added
      ...
      ```

  5. After the modification is complete, redeploy and verify the scheduling result.

      ```bash
      cd examples/deployer
      python deploy.py --config_dir <configuration directory>
      kubectl get pod -n <namespace> -o wide
      ```

      You can observe that each pod is scheduled to different nodes according to the label relationships.

  >[!NOTE]NOTE
  > In the PD co-location deployment scenario, deployment is performed by default through the `multi_deployment` method (the `user_config.json` must contain hybrid fields such as `hybrid_instances_num`), and there is no need to modify `infer_service_template.yaml`.
