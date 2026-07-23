{{- if .Values.configMap.create }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "mindie-pymotor.configMapName" . }}
  labels:
    {{- include "mindie-pymotor.labels" . | nindent 4 }}
data:
  user_config.json: |
{{ include "mindie-pymotor.renderJson" (dict "root" . "value" .Values.userConfig) | indent 4 }}
  env.json: |
{{ include "mindie-pymotor.renderJson" (dict "root" . "value" .Values.envConfig) | indent 4 }}
{{- end }}
