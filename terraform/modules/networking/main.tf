# =============================================================================
# MODULE: networking
# PURPOSE: Creates the Azure Virtual Network, Subnets, and Network Security
#          Groups. Everything in Azure lives inside a VNet — think of it as
#          your private data-center network in the cloud.
#
# WHAT YOU'LL LEARN:
#   - Azure VNets and CIDR addressing
#   - Subnet segmentation (AKS nodes vs services vs DB)
#   - Network Security Groups (firewalls for subnets)
#   - Service Endpoints (private connectivity to PaaS services)
# =============================================================================

# The VNet is the top-level networking container. All resources that need to
# communicate with each other must share (or be peered with) a VNet.
resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Address space uses CIDR notation: 10.0.0.0/8 means IPs 10.0.0.0 - 10.255.255.255
  # We use /16 giving us 65,536 addresses — plenty for learning!
  address_space = [var.vnet_address_space]

  tags = var.tags
}

# ─── Subnets ─────────────────────────────────────────────────────────────────
# Subnets divide the VNet into smaller segments. Best practice: one subnet per
# "tier" of your architecture so you can apply different security rules.

# AKS nodes live here. /22 = 1024 addresses (nodes + pods need lots of IPs!)
resource "azurerm_subnet" "aks" {
  name                 = "snet-aks-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.aks_subnet_cidr]

  # Service endpoints allow AKS pods to reach Azure PaaS services privately
  # without going through the public internet.
  service_endpoints = [
    "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault",
    "Microsoft.Sql",
  ]
}

# Database subnet — isolated from AKS for security
resource "azurerm_subnet" "database" {
  name                 = "snet-db-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.db_subnet_cidr]

  # PostgreSQL Flexible Server requires delegation so Azure can manage
  # network interfaces inside this subnet on your behalf.
  delegation {
    name = "postgres-delegation"
    service_delegation {
      name = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
      ]
    }
  }
}

# Application Gateway subnet — public-facing traffic lands here first
resource "azurerm_subnet" "appgw" {
  name                 = "snet-appgw-${var.environment}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.appgw_subnet_cidr]
}

# ─── Network Security Groups ──────────────────────────────────────────────────
# NSGs are stateful firewalls attached to subnets. They filter traffic by
# source/destination IP, port, and protocol.

resource "azurerm_network_security_group" "aks" {
  name                = "nsg-aks-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Allow HTTPS inbound from the Application Gateway subnet only
  security_rule {
    name                       = "Allow-HTTPS-From-AppGW"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = var.appgw_subnet_cidr
    destination_address_prefix = "*"
  }

  # Allow HTTP (will be redirected to HTTPS by ingress controller)
  security_rule {
    name                       = "Allow-HTTP-From-AppGW"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = var.appgw_subnet_cidr
    destination_address_prefix = "*"
  }

  tags = var.tags
}

resource "azurerm_network_security_group" "database" {
  name                = "nsg-db-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Only allow PostgreSQL port from AKS subnet — DB is NOT publicly accessible
  security_rule {
    name                       = "Allow-Postgres-From-AKS"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5432"
    source_address_prefix      = var.aks_subnet_cidr
    destination_address_prefix = "*"
  }

  # Deny ALL other inbound traffic explicitly
  security_rule {
    name                       = "Deny-All-Inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = var.tags
}

# ─── NSG Associations ────────────────────────────────────────────────────────
# Attach each NSG to its respective subnet

resource "azurerm_subnet_network_security_group_association" "aks" {
  subnet_id                 = azurerm_subnet.aks.id
  network_security_group_id = azurerm_network_security_group.aks.id
}

resource "azurerm_subnet_network_security_group_association" "database" {
  subnet_id                 = azurerm_subnet.database.id
  network_security_group_id = azurerm_network_security_group.database.id
}

# ─── Private DNS Zone ─────────────────────────────────────────────────────────
# PostgreSQL Flexible Server with private networking needs a private DNS zone
# so the AKS pods can resolve the database hostname.

resource "azurerm_private_dns_zone" "postgres" {
  name                = "${var.project_name}-${var.environment}.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "dns-link-postgres-${var.environment}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
  tags                  = var.tags
}
