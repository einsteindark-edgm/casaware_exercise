# ── ElastiCache Redis (Phase A.4) ────────────────────────────────────
#
# Single-node Redis 7 en t4g.micro, dentro de VPC privada.
# No encryption in-transit por simplicidad dev (sería `transit_encryption_enabled`).

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.prefix}-redis-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.prefix}-redis-subnets" }
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "${var.prefix}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]
}
