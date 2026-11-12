{{- if .Values.configMap.create }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "mindie-motor.configMapName" . }}
  labels:
    {{- include "mindie-motor.labels" . | nindent 4 }}
data:
  user_config.json: |
{{- if .Values.scalingPolicy }}
{{ include "mindie-motor.userConfig" . | indent 4 }}
{{- else }}
{{ include "mindie-motor.renderJson" (dict "root" . "value" .Values.userConfig) | indent 4 }}
{{- end }}
  env.json: |
{{ include "mindie-motor.renderJson" (dict "root" . "value" .Values.envConfig) | indent 4 }}
{{- end }}
