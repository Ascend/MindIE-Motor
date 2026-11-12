{{- define "mindie-motor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindie-motor.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "mindie-motor.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "mindie-motor.labels" -}}
app.kubernetes.io/name: {{ include "mindie-motor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "mindie-motor.serviceAccountName" -}}
{{- default (printf "%s-deployer" (include "mindie-motor.fullname" .) | trunc 63 | trimSuffix "-") .Values.serviceAccount.name -}}
{{- end -}}

{{- define "mindie-motor.rbacName" -}}
{{- printf "%s-%s" (include "mindie-motor.fullname" . | trunc 54 | trimSuffix "-") (.Release.Namespace | sha256sum | trunc 8) -}}
{{- end -}}

{{- define "mindie-motor.configMapName" -}}
{{- default (printf "%s-user-config" (include "mindie-motor.fullname" .) | trunc 63 | trimSuffix "-") .Values.configMap.name -}}
{{- end -}}

{{- define "mindie-motor.targetNamespace" -}}
{{- $userConfig := include "mindie-motor.renderJson" (dict "root" . "value" .Values.userConfig) | fromJson -}}
{{- required "userConfig.motor_deploy_config.job_id is required" (dig "motor_deploy_config" "job_id" "" $userConfig) -}}
{{- end -}}

{{- define "mindie-motor.jobName" -}}
{{- printf "%s-%s" (include "mindie-motor.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindie-motor.renderJson" -}}
{{- if kindIs "string" .value -}}
{{- tpl .value .root -}}
{{- else -}}
{{- .value | toPrettyJson -}}
{{- end -}}
{{- end -}}

{{- define "mindie-motor.userConfig" -}}
{{- $userConfig := include "mindie-motor.renderJson" (dict "root" . "value" .Values.userConfig) | fromJson -}}
{{- if .Values.scalingPolicy -}}
{{- $_ := set $userConfig "scaling_policy" .Values.scalingPolicy -}}
{{- end -}}
{{- $userConfig | toPrettyJson -}}
{{- end -}}
