# ── Security Groups (Phase A.1/A.4/A.8) ──────────────────────────────
#
# ALB (public) → Backend/Frontend (private) → Redis (private).

resource "aws_security_group" "alb" {
  name        = "${var.prefix}-alb-sg"
  description = "Allow 80/443 from the internet to the ALB."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-alb-sg" }
}

resource "aws_security_group" "backend" {
  name        = "${var.prefix}-backend-sg"
  description = "Backend ECS tasks - allow 8000 from ALB only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-backend-sg" }
}

resource "aws_security_group" "frontend" {
  name        = "${var.prefix}-frontend-sg"
  description = "Frontend ECS tasks - allow 3000 from ALB only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Next.js port from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-frontend-sg" }
}

resource "aws_security_group" "worker" {
  name        = "${var.prefix}-worker-sg"
  description = "Temporal workers - egress only (no inbound traffic needed)."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-worker-sg" }
}

resource "aws_security_group" "redis" {
  name        = "${var.prefix}-redis-sg"
  description = "ElastiCache Redis - allow 6379 from backend ECS only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from backend"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.backend.id]
  }

  ingress {
    description     = "Redis from workers"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-redis-sg" }
}
