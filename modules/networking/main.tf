locals {
  az_count = length(var.azs)

  # Each tier gets one subnet per AZ.
  # Public  netnum offsets: 0, 1, 2, ...
  # Private netnum offsets: 10, 11, 12, ...  (gap keeps future expansion clean)
  # Database netnum offsets: 20, 21, 22, ...
  public_netnum_offset   = 0
  private_netnum_offset  = 10
  database_netnum_offset = 20
}

# ── VPC ────────────────────────────────────────────────────────────────────────

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-vpc"
  })
}

# ── Subnets ───────────────────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, var.public_subnet_newbits, local.public_netnum_offset + count.index)
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-public-${var.azs[count.index]}"
    Tier = "public"
  })
}

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, var.private_subnet_newbits, local.private_netnum_offset + count.index)
  availability_zone = var.azs[count.index]

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-private-${var.azs[count.index]}"
    Tier = "private"
  })
}

resource "aws_subnet" "database" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, var.database_subnet_newbits, local.database_netnum_offset + count.index)
  availability_zone = var.azs[count.index]

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-database-${var.azs[count.index]}"
    Tier = "database"
  })
}

# ── Internet Gateway ───────────────────────────────────────────────────────────

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-igw"
  })
}

# ── Elastic IPs for NAT ────────────────────────────────────────────────────────

resource "aws_eip" "nat" {
  # One EIP per NAT Gateway. If single_nat_gateway=true, only one EIP.
  count  = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : local.az_count) : 0
  domain = "vpc"

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-nat-eip-${count.index}"
  })

  depends_on = [aws_internet_gateway.this]
}

# ── NAT Gateways ──────────────────────────────────────────────────────────────
# One per AZ (or one total if single_nat_gateway=true).
# Each NAT sits in the public subnet of its own AZ so private route tables
# can point to the AZ-local NAT — no cross-AZ NAT traffic.

resource "aws_nat_gateway" "this" {
  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : local.az_count) : 0

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-nat-${count.index}"
  })

  depends_on = [aws_internet_gateway.this]
}

# ── Route Tables ──────────────────────────────────────────────────────────────

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-rt-public"
  })
}

resource "aws_route_table_association" "public" {
  count          = local.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One private route table per AZ so each points to its AZ-local NAT Gateway.
# If single_nat_gateway=true, all private route tables point to the single NAT.
resource "aws_route_table" "private" {
  count  = local.az_count
  vpc_id = aws_vpc.this.id

  tags = merge(var.common_tags, {
    Name = "${var.project_name}-${var.environment}-rt-private-${var.azs[count.index]}"
  })
}

resource "aws_route" "private_nat" {
  count = var.enable_nat_gateway ? local.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  # single_nat_gateway collapses all routes to NAT index 0
  nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.this[0].id : aws_nat_gateway.this[count.index].id
}

resource "aws_route_table_association" "private" {
  count          = local.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Database subnets share the private route tables (no direct internet egress needed).
resource "aws_route_table_association" "database" {
  count          = local.az_count
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ── Security Groups ────────────────────────────────────────────────────────────
# SGs are defined with no inline rules to avoid circular dependency issues
# (e.g. sg_app references sg_alb and sg_db references sg_app).
# All ingress/egress rules are separate resources below.

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-${var.environment}-sg-alb"
  description = "ALB: accepts HTTP/HTTPS from internet"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.common_tags, { Name = "${var.project_name}-${var.environment}-sg-alb" })

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "app" {
  name        = "${var.project_name}-${var.environment}-sg-app"
  description = "App tier EC2: accepts traffic from ALB only"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.common_tags, { Name = "${var.project_name}-${var.environment}-sg-app" })

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "ml" {
  name        = "${var.project_name}-${var.environment}-sg-ml"
  description = "ML inference tier EC2: accepts traffic from ALB only"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.common_tags, { Name = "${var.project_name}-${var.environment}-sg-ml" })

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "db" {
  name        = "${var.project_name}-${var.environment}-sg-db"
  description = "RDS: accepts Postgres from app and ml tiers only"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.common_tags, { Name = "${var.project_name}-${var.environment}-sg-db" })

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "cache" {
  name        = "${var.project_name}-${var.environment}-sg-cache"
  description = "ElastiCache: accepts Redis from app and ml tiers only"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.common_tags, { Name = "${var.project_name}-${var.environment}-sg-cache" })

  lifecycle { create_before_destroy = true }
}

# ── Security Group Rules ───────────────────────────────────────────────────────

# ALB — inbound
resource "aws_security_group_rule" "alb_ingress_http" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "HTTP from internet (redirected to HTTPS by listener)"
}

resource "aws_security_group_rule" "alb_ingress_https" {
  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "HTTPS from internet"
}

resource "aws_security_group_rule" "alb_egress" {
  type              = "egress"
  security_group_id = aws_security_group.alb.id
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "ALB egress to app and ml target groups"
}

# App tier — inbound from ALB only
resource "aws_security_group_rule" "app_ingress_alb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.app.id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  description              = "App traffic from ALB"
}

resource "aws_security_group_rule" "app_egress" {
  type              = "egress"
  security_group_id = aws_security_group.app.id
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "App tier egress (RDS, ElastiCache, S3, Secrets Manager)"
}

# ML tier — inbound from ALB only
resource "aws_security_group_rule" "ml_ingress_alb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.ml.id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  description              = "ML inference traffic from ALB"
}

resource "aws_security_group_rule" "ml_egress" {
  type              = "egress"
  security_group_id = aws_security_group.ml.id
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "ML tier egress (S3 artefacts, ElastiCache, RDS, CloudWatch)"
}

# RDS — inbound from app and ml tiers only
resource "aws_security_group_rule" "db_ingress_app" {
  type                     = "ingress"
  security_group_id        = aws_security_group.db.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.app.id
  description              = "Postgres from app tier"
}

resource "aws_security_group_rule" "db_ingress_ml" {
  type                     = "ingress"
  security_group_id        = aws_security_group.db.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ml.id
  description              = "Postgres from ml tier"
}

resource "aws_security_group_rule" "db_egress" {
  type              = "egress"
  security_group_id = aws_security_group.db.id
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "RDS egress for replication and AWS services"
}

# ElastiCache — inbound from app and ml tiers only
resource "aws_security_group_rule" "cache_ingress_app" {
  type                     = "ingress"
  security_group_id        = aws_security_group.cache.id
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.app.id
  description              = "Redis from app tier"
}

resource "aws_security_group_rule" "cache_ingress_ml" {
  type                     = "ingress"
  security_group_id        = aws_security_group.cache.id
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ml.id
  description              = "Redis from ml tier (online feature store lookups)"
}

resource "aws_security_group_rule" "cache_egress" {
  type              = "egress"
  security_group_id = aws_security_group.cache.id
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "ElastiCache egress"
}
