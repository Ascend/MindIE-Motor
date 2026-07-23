{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "mindie-pymotor.serviceAccountName" . }}
  labels:
    {{- include "mindie-pymotor.labels" . | nindent 4 }}
{{- end }}
