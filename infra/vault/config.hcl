ui = true
# Local Docker hosts often cannot satisfy Vault's mlock requirement even with
# IPC_LOCK. Production deployments should prefer mlock where supported.
disable_mlock = true

storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
