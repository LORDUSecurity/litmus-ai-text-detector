-- AI Text Detection Pipeline Database Schema

-- Enable UUID extension if not already enabled
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Detector Versions Table (referenced by analysis_results)
CREATE TABLE detector_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version TEXT NOT NULL,
    detector_version TEXT,
    feature_schema_version TEXT,
    classifier_version TEXT,
    threshold_version TEXT,
    release_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    description TEXT
);

-- Analysis Results Table
CREATE TABLE analysis_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id TEXT,
    text_hash TEXT NOT NULL,
    classification TEXT CHECK (classification IN ('AI_GENERATED', 'HUMAN_LIKELY', 'REVIEW_REQUIRED', 'DETECTION_UNAVAILABLE')),
    ai_probability DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    method TEXT,
    provenance_json JSONB,
    watermark_json JSONB,
    features_json JSONB,
    model_results_json JSONB,
    review_required BOOLEAN,
    review_status TEXT DEFAULT 'pending',
    detector_version TEXT,
    feature_schema_version TEXT,
    classifier_version TEXT,
    threshold_version TEXT,
    version_id UUID REFERENCES detector_versions(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Review Labels Table
CREATE TABLE review_labels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    analysis_result_id UUID REFERENCES analysis_results(id),
    document_id TEXT,
    text_hash TEXT NOT NULL,
    automatic_classification TEXT,
    automatic_probability DOUBLE PRECISION,
    automatic_confidence DOUBLE PRECISION,
    reviewer_label TEXT CHECK (reviewer_label IN ('AI', 'HUMAN', 'INCONCLUSIVE', 'MIXED', 'INVALID')),
    reviewer_id TEXT,
    reviewer_notes TEXT,
    review_confidence TEXT,
    text_sample TEXT,
    features_json JSONB,
    model_scores_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Provider Usage Table
CREATE TABLE provider_usage (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_name TEXT NOT NULL,
    model_name TEXT,
    request_count INTEGER DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    response_time_ms INTEGER DEFAULT 0,
    status_code INTEGER,
    error_occurred BOOLEAN DEFAULT FALSE,
    rate_limit_event BOOLEAN DEFAULT FALSE,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_analysis_results_text_hash ON analysis_results(text_hash);
CREATE INDEX idx_analysis_results_classification ON analysis_results(classification);
CREATE INDEX idx_analysis_results_review_status ON analysis_results(review_status);
CREATE INDEX idx_review_labels_analysis_result_id ON review_labels(analysis_result_id);
CREATE INDEX idx_review_labels_document_id ON review_labels(document_id);
CREATE INDEX idx_provider_usage_provider_timestamp ON provider_usage(provider_name, timestamp);

-- Useful Views

-- Review queue view
CREATE VIEW review_queue AS
SELECT 
    ar.id as analysis_result_id,
    ar.document_id,
    ar.text_hash,
    ar.classification as automatic_classification,
    ar.ai_probability,
    ar.confidence,
    ar.method,
    ar.created_at as analysis_created_at
FROM analysis_results ar
WHERE ar.review_required = true
  AND ar.review_status = 'pending'
ORDER BY ar.created_at DESC;

-- Provider performance view
CREATE VIEW provider_performance AS
SELECT 
    provider_name,
    model_name,
    COUNT(*) as total_requests,
    AVG(response_time_ms) as avg_response_time_ms,
    SUM(CASE WHEN rate_limit_event THEN 1 ELSE 0 END) as rate_limit_count,
    SUM(CASE WHEN error_occurred THEN 1 ELSE 0 END) as error_count,
    MAX(timestamp) as last_request
FROM provider_usage
GROUP BY provider_name, model_name
ORDER BY total_requests DESC;

-- Cache statistics view
CREATE VIEW cache_statistics AS
SELECT 
    DATE(created_at) as date,
    COUNT(*) as total_analyses,
    COUNT(*) FILTER (WHERE classification = 'AI_GENERATED') as ai_count,
    COUNT(*) FILTER (WHERE classification = 'HUMAN_LIKELY') as human_count,
    COUNT(*) FILTER (WHERE classification = 'REVIEW_REQUIRED') as review_count,
    COUNT(*) FILTER (WHERE review_required = true) as review_required_count
FROM analysis_results
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- Model agreement statistics view
CREATE VIEW model_agreement_stats AS
SELECT 
    DATE(created_at) as date,
    AVG(ai_probability) as avg_ai_probability,
    AVG(confidence) as avg_confidence,
    COUNT(*) as total
FROM analysis_results
WHERE model_results_json IS NOT NULL
GROUP BY DATE(created_at)
ORDER BY date DESC;