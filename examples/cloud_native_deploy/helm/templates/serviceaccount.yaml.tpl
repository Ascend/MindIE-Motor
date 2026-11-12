{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "mindie-motor.serviceAccountName" . }}
  labels:
    {{- include "mindie-motor.labels" . | nindent 4 }}
{{- end }}
