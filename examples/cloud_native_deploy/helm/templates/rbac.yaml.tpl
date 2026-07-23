{{- if .Values.rbac.create }}
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ include "mindie-pymotor.rbacName" . }}
  labels:
    {{- include "mindie-pymotor.labels" . | nindent 4 }}
rules:
  - apiGroups: [""]
    resources: ["configmaps", "serviceaccounts"]
    verbs: ["get", "create", "patch", "delete"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "create", "patch", "delete"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["list"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "create", "patch", "delete"]
  - apiGroups: ["mindcluster.huawei.com"]
    resources: ["inferservicesets"]
    verbs: ["get", "list", "create", "patch", "delete"]
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterroles"]
    verbs: ["get", "create", "patch", "delete"]
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterrolebindings"]
    verbs: ["get", "create", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ include "mindie-pymotor.rbacName" . }}
  labels:
    {{- include "mindie-pymotor.labels" . | nindent 4 }}
subjects:
  - kind: ServiceAccount
    name: {{ include "mindie-pymotor.serviceAccountName" . }}
    namespace: {{ .Release.Namespace }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {{ include "mindie-pymotor.rbacName" . }}
{{- end }}
