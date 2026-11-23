#!/bin/bash

# Performance Testing Script for TuneIO
# This script generates traffic and displays performance metrics

echo "🚀 TuneIO - Testing Script"
echo "=============================================="
echo ""

BASE_URL="http://localhost:3000"

# Check if server is running
echo "📡 Checking if server is running..."
if ! curl -s "$BASE_URL/health" > /dev/null; then
    echo "❌ Server is not running. Please start it with: npm start"
    exit 1
fi
echo "✅ Server is running!"
echo ""

# Clear existing metrics
echo "🧹 Clearing existing metrics..."
curl -s -X DELETE "$BASE_URL/metrics" > /dev/null
echo "✅ Metrics cleared"
echo ""

# Generate traffic
echo "🔄 Generating traffic (15 requests)..."
for i in {1..5}; do
    echo "  Request batch $i/5..."
    curl -s "$BASE_URL/users" > /dev/null
    curl -s "$BASE_URL/products" > /dev/null
    curl -s "$BASE_URL/orders" > /dev/null
done
echo "✅ Traffic generated"
echo ""

# Display metrics
echo "📊 Performance Metrics:"
echo "======================"
echo ""

echo "1️⃣  All Metrics:"
curl -s "$BASE_URL/metrics" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/metrics"
echo ""
echo ""

echo "2️⃣  Slow Requests (> 500ms):"
curl -s "$BASE_URL/metrics/slow" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/metrics/slow"
echo ""
echo ""

echo "3️⃣  Performance Statistics:"
curl -s "$BASE_URL/metrics/stats" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/metrics/stats"
echo ""
echo ""

echo "4️⃣  Performance Analysis & Insights:"
curl -s "$BASE_URL/analysis" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/analysis"
echo ""
echo ""

echo "5️⃣  Critical Issues Only:"
curl -s "$BASE_URL/analysis/critical" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/analysis/critical"
echo ""
echo ""

echo "6️⃣  Cache Statistics:"
curl -s "$BASE_URL/cache/stats" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/cache/stats"
echo ""
echo ""

echo "7️⃣  Performance Comparison (Cached vs Non-Cached):"
curl -s "$BASE_URL/metrics/comparison" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/metrics/comparison"
echo ""
echo ""

echo "✅ Testing complete!"
echo ""
echo "💡 You can also test manually:"
echo "   - All metrics:      curl $BASE_URL/metrics"
echo "   - Slow requests:    curl $BASE_URL/metrics/slow"
echo "   - Statistics:       curl $BASE_URL/metrics/stats"
echo "   - All insights:     curl $BASE_URL/analysis"
echo "   - Critical issues:  curl $BASE_URL/analysis/critical"
echo "   - Analysis summary: curl $BASE_URL/analysis/summary"
echo "   - Cache stats:      curl $BASE_URL/cache/stats"
echo "   - Cache comparison: curl $BASE_URL/metrics/comparison"
echo "   - Clear cache:      curl -X DELETE $BASE_URL/cache/clear"
