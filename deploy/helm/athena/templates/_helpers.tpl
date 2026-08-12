{{- define "athena.name" -}}
athena
{{- end -}}

{{- define "athena.labels" -}}
app.kubernetes.io/name: {{ include "athena.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
