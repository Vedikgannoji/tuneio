# tuneio Dashboard

A clean, modern web dashboard for visualizing API performance metrics and insights.

## Features

- **Real-time Stats**: Total requests, average response time, slow requests, and cache improvement
- **Performance Chart**: Bar chart showing endpoint response times with color coding
- **Slow APIs List**: Detailed view of endpoints exceeding performance thresholds
- **Analysis & Suggestions**: AI-powered recommendations with severity levels
- **Cache Comparison**: Visual comparison of performance with and without caching
- **Auto-refresh**: Dashboard updates every 30 seconds automatically

## How to Use

1. Start the server:
   ```bash
   npm start
   ```

2. Open your browser and navigate to:
   ```
   http://localhost:3000
   ```

3. The dashboard will automatically load data from the backend APIs

4. Click the "Refresh Data" button to manually reload all metrics

## Color Coding

- **Green**: Good performance (< 500ms)
- **Yellow**: Warning (500ms - 1000ms)
- **Red**: Slow performance (> 1000ms)

## API Endpoints Used

- `/metrics` - Overall performance metrics
- `/metrics/stats` - Summary statistics
- `/metrics/slow` - Slow endpoint details
- `/analysis` - Performance analysis and suggestions
- `/metrics/comparison` - Cache performance comparison

## Technologies

- Pure HTML5, CSS3, JavaScript (no frameworks)
- Chart.js for data visualization
- Responsive design for mobile and desktop
