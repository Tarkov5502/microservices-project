variable "project_name"       { type = string; default = "microservices" }
variable "environment"        { type = string; default = "dev" }
variable "location"           { type = string; default = "eastus2" }
variable "kubernetes_version" { type = string; default = "1.29" }
variable "tags"               { type = map(string); default = {} }
