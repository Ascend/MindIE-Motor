{{- if and .Values.jobs.deploy.enabled (eq .Values.operation "deploy") }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "mindie-pymotor.jobName" (dict "root" . "name" .Values.jobs.deploy.name) }}
  labels:
    {{- include "mindie-pymotor.labels" . | nindent 4 }}
  annotations:
    # 在 Helm 首次安装或版本升级完成后执行该 Job，保证 rbac 等资源先创建。
    helm.sh/hook: post-install,post-upgrade
    # 再次创建同名 Hook Job 前删除旧 Job，避免名称冲突。
    helm.sh/hook-delete-policy: before-hook-creation
spec:
  ttlSecondsAfterFinished: {{ .Values.jobs.ttlSecondsAfterFinished }}
  backoffLimit: {{ .Values.jobs.backoffLimit }}
  template:
    metadata:
      labels:
        {{- include "mindie-pymotor.labels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "mindie-pymotor.serviceAccountName" . }}
      restartPolicy: Never
      {{- with .Values.image.pullSecrets }}
      imagePullSecrets:
        {{- range . }}
        - name: {{ . }}
        {{- end }}
      {{- end }}
      containers:
        - name: deployer
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - name: CONFIG_DIR
              value: {{ .Values.jobs.configDir | quote }}
          volumeMounts:
            - name: deploy-config
              mountPath: {{ .Values.jobs.configDir | quote }}
              readOnly: true
      volumes:
        - name: deploy-config
          configMap:
            name: {{ include "mindie-pymotor.configMapName" . }}
            items:
              - key: user_config.json
                path: user_config.json
              - key: env.json
                path: env.json
{{- end }}
