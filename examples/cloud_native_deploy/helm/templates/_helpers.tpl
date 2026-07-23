{{- define "mindie-pymotor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindie-pymotor.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "mindie-pymotor.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "mindie-pymotor.labels" -}}
app.kubernetes.io/name: {{ include "mindie-pymotor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "mindie-pymotor.serviceAccountName" -}}
{{- default (printf "%s-deployer" (include "mindie-pymotor.fullname" .) | trunc 63 | trimSuffix "-") .Values.serviceAccount.name -}}
{{- end -}}

{{- define "mindie-pymotor.rbacName" -}}
{{- printf "%s-%s" (include "mindie-pymotor.fullname" . | trunc 54 | trimSuffix "-") (.Release.Namespace | sha256sum | trunc 8) -}}
{{- end -}}

{{- define "mindie-pymotor.configMapName" -}}
{{- default (printf "%s-user-config" (include "mindie-pymotor.fullname" .) | trunc 63 | trimSuffix "-") .Values.configMap.name -}}
{{- end -}}

{{- define "mindie-pymotor.targetNamespace" -}}
{{- $userConfig := include "mindie-pymotor.renderJson" (dict "root" . "value" .Values.userConfig) | fromJson -}}
{{- required "userConfig.motor_deploy_config.job_id is required" (dig "motor_deploy_config" "job_id" "" $userConfig) -}}
{{- end -}}

{{- define "mindie-pymotor.jobName" -}}
{{- printf "%s-%s" (include "mindie-pymotor.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindie-pymotor.renderJson" -}}
{{- if kindIs "string" .value -}}
{{- tpl .value .root -}}
{{- else -}}
{{- .value | toPrettyJson -}}
{{- end -}}
{{- end -}}
