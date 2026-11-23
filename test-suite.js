/**
 * Comprehensive Test Suite for tuneio
 * Tests backend APIs, performance tracking, caching, and data accuracy
 */

const BASE_URL = 'http://localhost:3000';

// Test results storage
const testResults = {
    passed: [],
    failed: [],
    warnings: []
};

// Helper function to make HTTP requests
async function makeRequest(endpoint, method = 'GET', body = null) {
    const options = {
        method,
        headers: { 'Content-Type': 'application/json' }
    };
    
    if (body) {
        options.body = JSON.stringify(body);
    }
    
    const startTime = Date.now();
    const response = await fetch(`${BASE_URL}${endpoint}`, options);
    const responseTime = Date.now() - startTime;
    const data = await response.json();
    
    return { response, data, responseTime };
}

// Helper function to log test results
function logTest(testName, passed, message = '') {
    if (passed) {
        testResults.passed.push({ test: testName, message });
        console.log(`✅ PASS: ${testName}${message ? ' - ' + message : ''}`);
    } else {
        testResults.failed.push({ test: testName, message });
        console.log(`❌ FAIL: ${testName}${message ? ' - ' + message : ''}`);
    }
}

function logWarning(testName, message) {
    testResults.warnings.push({ test: testName, message });
    console.log(`⚠️  WARN: ${testName} - ${message}`);
}

// Test 1: Backend API Testing
async function testBasicAPIs() {
    console.log('\n=== TEST 1: BASIC API ENDPOINTS ===\n');
    
    // Test /users
    try {
        const { response, data } = await makeRequest('/users');
        logTest('GET /users', response.status === 200 && data.success, `Returned ${data.data?.length || 0} users`);
        logTest('GET /users - JSON structure', data.hasOwnProperty('success') && data.hasOwnProperty('data'));
    } catch (error) {
        logTest('GET /users', false, error.message);
    }
    
    // Test /products
    try {
        const { response, data } = await makeRequest('/products');
        logTest('GET /products', response.status === 200 && data.success, `Returned ${data.data?.length || 0} products`);
        logTest('GET /products - JSON structure', data.hasOwnProperty('success') && data.hasOwnProperty('data'));
    } catch (error) {
        logTest('GET /products', false, error.message);
    }
    
    // Test /orders
    try {
        const { response, data } = await makeRequest('/orders');
        logTest('GET /orders', response.status === 200 && data.success, `Returned ${data.data?.length || 0} orders`);
        logTest('GET /orders - JSON structure', data.hasOwnProperty('success') && data.hasOwnProperty('data'));
    } catch (error) {
        logTest('GET /orders', false, error.message);
    }
    
    // Test /health
    try {
        const { response, data } = await makeRequest('/health');
        logTest('GET /health', response.status === 200 && data.status === 'healthy');
    } catch (error) {
        logTest('GET /health', false, error.message);
    }
}

// Test 2: Performance Tracking
async function testPerformanceTracking() {
    console.log('\n=== TEST 2: PERFORMANCE TRACKING ===\n');
    
    // Clear existing metrics first
    try {
        await makeRequest('/metrics', 'DELETE');
        console.log('📝 Cleared existing metrics\n');
    } catch (error) {
        console.log('⚠️  Could not clear metrics:', error.message);
    }
    
    // Make multiple requests
    console.log('📝 Making 20 test requests...\n');
    const requestCount = 20;
    
    for (let i = 0; i < requestCount; i++) {
        const endpoints = ['/users', '/products', '/orders'];
        const endpoint = endpoints[i % 3];
        await makeRequest(endpoint);
        
        if ((i + 1) % 5 === 0) {
            console.log(`   Made ${i + 1}/${requestCount} requests...`);
        }
    }
    
    // Wait a bit for logs to be recorded
    await new Promise(resolve => setTimeout(resolve, 500));
    
    // Check metrics
    try {
        const { data } = await makeRequest('/metrics');
        
        logTest('Metrics logging enabled', data.success && data.count > 0, `Logged ${data.count} requests`);
        
        if (data.data && data.data.length > 0) {
            const log = data.data[0];
            
            logTest('Log contains endpoint', log.hasOwnProperty('endpoint'));
            logTest('Log contains responseTime', log.hasOwnProperty('responseTime') && log.responseTime > 0, `${log.responseTime}ms`);
            logTest('Log contains statusCode', log.hasOwnProperty('statusCode'));
            logTest('Log contains timestamp', log.hasOwnProperty('timestamp'));
            logTest('Log contains method', log.hasOwnProperty('method'));
            
            // Check for different endpoints
            const uniqueEndpoints = [...new Set(data.data.map(l => l.endpoint))];
            logTest('Multiple endpoints tracked', uniqueEndpoints.length >= 3, `Tracked: ${uniqueEndpoints.join(', ')}`);
            
            // Check response times are realistic
            const avgTime = data.data.reduce((sum, l) => sum + l.responseTime, 0) / data.data.length;
            logTest('Response times are realistic', avgTime > 0 && avgTime < 10000, `Avg: ${avgTime.toFixed(2)}ms`);
        }
    } catch (error) {
        logTest('Performance tracking', false, error.message);
    }
}

// Test 3: Metrics Stats
async function testMetricsStats() {
    console.log('\n=== TEST 3: METRICS STATS ===\n');
    
    try {
        const { data } = await makeRequest('/metrics/stats');
        
        logTest('GET /metrics/stats', data.success);
        
        if (data.success && data.data) {
            const stats = data.data;
            
            logTest('Stats - totalRequests', typeof stats.totalRequests === 'number' && stats.totalRequests > 0, `${stats.totalRequests} requests`);
            logTest('Stats - averageResponseTime', typeof stats.averageResponseTime === 'number' && stats.averageResponseTime > 0, `${stats.averageResponseTime.toFixed(2)}ms`);
            logTest('Stats - slowRequestCount', typeof stats.slowRequestCount === 'number' && stats.slowRequestCount >= 0, `${stats.slowRequestCount} slow requests`);
            logTest('Stats - endpointStats exists', stats.hasOwnProperty('endpointStats'));
            
            // Validate endpoint stats
            if (stats.endpointStats) {
                const endpointKeys = Object.keys(stats.endpointStats);
                logTest('Endpoint stats populated', endpointKeys.length > 0, `${endpointKeys.length} endpoints`);
                
                if (endpointKeys.length > 0) {
                    const firstEndpoint = stats.endpointStats[endpointKeys[0]];
                    logTest('Endpoint stats structure', 
                        firstEndpoint.hasOwnProperty('count') && 
                        firstEndpoint.hasOwnProperty('avgTime') &&
                        firstEndpoint.hasOwnProperty('minTime') &&
                        firstEndpoint.hasOwnProperty('maxTime')
                    );
                }
            }
        }
    } catch (error) {
        logTest('GET /metrics/stats', false, error.message);
    }
}

// Test 4: Slow API Detection
async function testSlowAPIDetection() {
    console.log('\n=== TEST 4: SLOW API DETECTION ===\n');
    
    try {
        const { data } = await makeRequest('/metrics/slow?threshold=500');
        
        logTest('GET /metrics/slow', data.success);
        logTest('Slow APIs - returns array', Array.isArray(data.data));
        
        if (data.data.length > 0) {
            console.log(`   Found ${data.data.length} slow requests`);
            
            // Validate all slow requests are actually slow
            const allSlow = data.data.every(log => log.responseTime > 500);
            logTest('Slow APIs - all above threshold', allSlow);
            
            // Check structure
            const firstSlow = data.data[0];
            logTest('Slow API structure', 
                firstSlow.hasOwnProperty('endpoint') &&
                firstSlow.hasOwnProperty('responseTime') &&
                firstSlow.hasOwnProperty('method')
            );
        } else {
            logWarning('Slow APIs', 'No slow APIs detected (this is OK if all requests are fast)');
        }
    } catch (error) {
        logTest('GET /metrics/slow', false, error.message);
    }
}

// Test 5: Analysis Engine
async function testAnalysisEngine() {
    console.log('\n=== TEST 5: ANALYSIS ENGINE ===\n');
    
    try {
        const { data } = await makeRequest('/analysis');
        
        logTest('GET /analysis', data.success);
        logTest('Analysis - returns insights array', Array.isArray(data.data));
        logTest('Analysis - has summary', data.hasOwnProperty('summary'));
        
        if (data.data && data.data.length > 0) {
            console.log(`   Generated ${data.data.length} insights`);
            
            // Check insight structure
            const insight = data.data[0];
            logTest('Insight structure', 
                insight.hasOwnProperty('endpoint') &&
                insight.hasOwnProperty('issue') &&
                insight.hasOwnProperty('severity') &&
                insight.hasOwnProperty('suggestion')
            );
            
            // Validate severity values
            const validSeverities = data.data.every(i => ['high', 'medium', 'low'].includes(i.severity));
            logTest('Severity values valid', validSeverities);
            
            // Check if suggestions are meaningful
            const hasSuggestions = data.data.every(i => i.suggestion && i.suggestion.length > 10);
            logTest('Suggestions are meaningful', hasSuggestions);
            
            // Validate summary
            if (data.summary) {
                logTest('Summary structure', 
                    data.summary.hasOwnProperty('totalInsights') &&
                    data.summary.hasOwnProperty('bySeverity')
                );
            }
        } else {
            logWarning('Analysis', 'No insights generated (this is OK if performance is good)');
        }
    } catch (error) {
        logTest('GET /analysis', false, error.message);
    }
    
    // Test critical insights
    try {
        const { data } = await makeRequest('/analysis/critical');
        logTest('GET /analysis/critical', data.success);
        logTest('Critical insights - returns array', Array.isArray(data.data));
    } catch (error) {
        logTest('GET /analysis/critical', false, error.message);
    }
}

// Test 6: Caching System
async function testCachingSystem() {
    console.log('\n=== TEST 6: CACHING SYSTEM ===\n');
    
    // Disable cache first
    try {
        const { data } = await makeRequest('/cache/disable', 'POST');
        logTest('POST /cache/disable', data.success);
        console.log('   Cache disabled\n');
    } catch (error) {
        logTest('POST /cache/disable', false, error.message);
    }
    
    // Make requests without cache
    console.log('📝 Making 5 requests WITHOUT cache...\n');
    const timesWithoutCache = [];
    for (let i = 0; i < 5; i++) {
        const { responseTime } = await makeRequest('/products');
        timesWithoutCache.push(responseTime);
    }
    const avgWithoutCache = timesWithoutCache.reduce((a, b) => a + b, 0) / timesWithoutCache.length;
    console.log(`   Avg response time WITHOUT cache: ${avgWithoutCache.toFixed(2)}ms\n`);
    
    // Enable cache
    try {
        const { data } = await makeRequest('/cache/enable', 'POST');
        logTest('POST /cache/enable', data.success);
        console.log('   Cache enabled\n');
    } catch (error) {
        logTest('POST /cache/enable', false, error.message);
    }
    
    // Make requests with cache
    console.log('📝 Making 5 requests WITH cache...\n');
    const timesWithCache = [];
    for (let i = 0; i < 5; i++) {
        const { responseTime } = await makeRequest('/products');
        timesWithCache.push(responseTime);
    }
    const avgWithCache = timesWithCache.reduce((a, b) => a + b, 0) / timesWithCache.length;
    console.log(`   Avg response time WITH cache: ${avgWithCache.toFixed(2)}ms\n`);
    
    // Validate cache improves performance
    const improvement = ((avgWithoutCache - avgWithCache) / avgWithoutCache) * 100;
    logTest('Cache improves performance', avgWithCache < avgWithoutCache, `${improvement.toFixed(1)}% faster`);
    
    // Test cache stats
    try {
        const { data } = await makeRequest('/cache/stats');
        logTest('GET /cache/stats', data.success);
    } catch (error) {
        logTest('GET /cache/stats', false, error.message);
    }
    
    // Test cache clear
    try {
        const { data } = await makeRequest('/cache/clear', 'DELETE');
        logTest('DELETE /cache/clear', data.success);
    } catch (error) {
        logTest('DELETE /cache/clear', false, error.message);
    }
}

// Test 7: Performance Comparison
async function testPerformanceComparison() {
    console.log('\n=== TEST 7: PERFORMANCE COMPARISON ===\n');
    
    try {
        const { data } = await makeRequest('/metrics/comparison');
        
        logTest('GET /metrics/comparison', data.success);
        
        if (data.success && data.data) {
            const comp = data.data;
            
            logTest('Comparison - totalRequests', typeof comp.totalRequests === 'number');
            logTest('Comparison - cachedRequests', typeof comp.cachedRequests === 'number');
            logTest('Comparison - nonCachedRequests', typeof comp.nonCachedRequests === 'number');
            logTest('Comparison - avgResponseTimeWithCache', typeof comp.avgResponseTimeWithCache === 'number');
            logTest('Comparison - avgResponseTimeWithoutCache', typeof comp.avgResponseTimeWithoutCache === 'number');
            logTest('Comparison - improvementPercentage', typeof comp.improvementPercentage === 'number');
            
            // Validate logical consistency
            if (comp.cachedRequests > 0 && comp.nonCachedRequests > 0) {
                logTest('Comparison - has both cached and non-cached data', true, 
                    `${comp.cachedRequests} cached, ${comp.nonCachedRequests} non-cached`);
                
                // Check if improvement makes sense
                if (comp.improvementPercentage > 0) {
                    logTest('Comparison - cache shows improvement', 
                        comp.avgResponseTimeWithCache < comp.avgResponseTimeWithoutCache,
                        `${comp.improvementPercentage.toFixed(1)}% improvement`
                    );
                }
            } else {
                logWarning('Comparison', 'Insufficient data for meaningful comparison');
            }
        }
    } catch (error) {
        logTest('GET /metrics/comparison', false, error.message);
    }
}

// Test 8: Data Consistency
async function testDataConsistency() {
    console.log('\n=== TEST 8: DATA CONSISTENCY ===\n');
    
    try {
        // Fetch all data sources
        const [metrics, stats, slow, analysis, comparison] = await Promise.all([
            makeRequest('/metrics'),
            makeRequest('/metrics/stats'),
            makeRequest('/metrics/slow'),
            makeRequest('/analysis'),
            makeRequest('/metrics/comparison')
        ]);
        
        // Check metrics count matches stats
        if (metrics.data.success && stats.data.success) {
            const metricsCount = metrics.data.count;
            const statsCount = stats.data.data.totalRequests;
            
            logTest('Consistency - metrics count matches stats', 
                metricsCount === statsCount,
                `Metrics: ${metricsCount}, Stats: ${statsCount}`
            );
        }
        
        // Check slow APIs are subset of all metrics
        if (metrics.data.success && slow.data.success) {
            const slowCount = slow.data.count;
            const totalCount = metrics.data.count;
            
            logTest('Consistency - slow APIs <= total requests', 
                slowCount <= totalCount,
                `Slow: ${slowCount}, Total: ${totalCount}`
            );
        }
        
        // Check analysis insights reference real endpoints
        if (analysis.data.success && metrics.data.success) {
            const endpoints = [...new Set(metrics.data.data.map(m => `${m.method} ${m.endpoint}`))];
            const insightEndpoints = analysis.data.data.map(i => i.endpoint);
            
            const allValid = insightEndpoints.every(ie => 
                endpoints.some(e => ie.includes(e.split(' ')[1]))
            );
            
            logTest('Consistency - analysis references real endpoints', allValid);
        }
        
    } catch (error) {
        logTest('Data consistency check', false, error.message);
    }
}

// Test 9: Edge Cases
async function testEdgeCases() {
    console.log('\n=== TEST 9: EDGE CASES ===\n');
    
    // Test with no data
    try {
        await makeRequest('/metrics', 'DELETE');
        console.log('📝 Cleared all metrics\n');
        
        const { data: stats } = await makeRequest('/metrics/stats');
        logTest('Edge case - empty stats', 
            stats.data.totalRequests === 0 &&
            stats.data.averageResponseTime === 0
        );
        
        const { data: slow } = await makeRequest('/metrics/slow');
        logTest('Edge case - no slow APIs', slow.data.length === 0);
        
        const { data: analysis } = await makeRequest('/analysis');
        logTest('Edge case - no insights', analysis.data.length === 0);
        
        const { data: comparison } = await makeRequest('/metrics/comparison');
        logTest('Edge case - no comparison data', 
            comparison.data.totalRequests === 0 &&
            comparison.data.improvementPercentage === 0
        );
        
    } catch (error) {
        logTest('Edge cases', false, error.message);
    }
    
    // Test 404
    try {
        const response = await fetch(`${BASE_URL}/nonexistent`);
        logTest('Edge case - 404 handling', response.status === 404);
    } catch (error) {
        logTest('Edge case - 404 handling', false, error.message);
    }
}

// Generate final report
function generateReport() {
    console.log('\n' + '='.repeat(60));
    console.log('TEST REPORT SUMMARY');
    console.log('='.repeat(60) + '\n');
    
    console.log(`✅ PASSED: ${testResults.passed.length} tests`);
    console.log(`❌ FAILED: ${testResults.failed.length} tests`);
    console.log(`⚠️  WARNINGS: ${testResults.warnings.length} warnings\n`);
    
    if (testResults.failed.length > 0) {
        console.log('FAILED TESTS:');
        testResults.failed.forEach(f => {
            console.log(`  ❌ ${f.test}${f.message ? ': ' + f.message : ''}`);
        });
        console.log('');
    }
    
    if (testResults.warnings.length > 0) {
        console.log('WARNINGS:');
        testResults.warnings.forEach(w => {
            console.log(`  ⚠️  ${w.test}: ${w.message}`);
        });
        console.log('');
    }
    
    const passRate = (testResults.passed.length / (testResults.passed.length + testResults.failed.length) * 100).toFixed(1);
    console.log(`PASS RATE: ${passRate}%\n`);
    
    if (testResults.failed.length === 0) {
        console.log('🎉 ALL TESTS PASSED! System is working correctly.\n');
    } else {
        console.log('⚠️  SOME TESTS FAILED. Review issues above.\n');
    }
}

// Run all tests
async function runAllTests() {
    console.log('🧪 tuneio - COMPREHENSIVE TEST SUITE');
    console.log('='.repeat(60));
    
    try {
        await testBasicAPIs();
        await testPerformanceTracking();
        await testMetricsStats();
        await testSlowAPIDetection();
        await testAnalysisEngine();
        await testCachingSystem();
        await testPerformanceComparison();
        await testDataConsistency();
        await testEdgeCases();
        
        generateReport();
    } catch (error) {
        console.error('❌ Test suite error:', error);
    }
}

// Run tests
runAllTests();
