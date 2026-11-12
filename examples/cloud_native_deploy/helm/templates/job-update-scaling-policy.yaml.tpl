{{- if eq .Values.operation "update-scaling-policy" }}
{{- if .Release.IsInstall }}
{{- fail "update-scaling-policy requires an existing release; use helm upgrade" }}
{{- end }}
{{- $update := required "policyUpdate is required" .Values.policyUpdate }}
{{- $roles := required "policyUpdate.roles is required" $update.roles }}
{{- $name := include "mindie-motor.jobName" (dict "root" . "name" "update-scaling-policy") }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $name }}
  labels:
    {{- include "mindie-motor.labels" . | nindent 4 }}
data:
  policy.json: |
    {{- dict "namespace" (include "mindie-motor.targetNamespace" .) "roles" $roles | toPrettyJson | nindent 4 }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ $name }}
  labels:
    {{- include "mindie-motor.labels" . | nindent 4 }}
  annotations:
    helm.sh/hook: post-upgrade
    helm.sh/hook-delete-policy: before-hook-creation
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: {{ .Values.jobs.ttlSecondsAfterFinished }}
  template:
    spec:
      serviceAccountName: {{ include "mindie-motor.serviceAccountName" . }}
      restartPolicy: Never
      {{- with .Values.jobs.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.image.pullSecrets }}
      imagePullSecrets:
        {{- range . }}
        - name: {{ . }}
        {{- end }}
      {{- end }}
      containers:
        - name: update-scaling-policy
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["python3", "/app/examples/deployer/deploy.py"]
          args: ["--update_scaling_policy", "/policy/policy.json"]
          volumeMounts:
            - name: policy
              mountPath: /policy
              readOnly: true
      volumes:
        - name: policy
          configMap:
            name: {{ $name }}
{{- end }}
