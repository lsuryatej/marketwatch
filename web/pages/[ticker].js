import { useRouter } from 'next/router';
import useSWR from 'swr';

const fetcher = (url) => fetch(url).then((res) => res.json());

export default function CompanyPage() {
  const router = useRouter();
  const { ticker } = router.query;
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || '';
  const { data, error } = useSWR(
    ticker ? `${apiBase}/api/v1/company/${ticker.toUpperCase()}/history` : null,
    fetcher
  );
  if (error) return <div>Error loading data</div>;
  if (!data) return <div>Loading...</div>;
  return (
    <main style={{ padding: '2rem', fontFamily: 'Arial, sans-serif' }}>
      <h1>History for {ticker?.toUpperCase()}</h1>
      <table border="1" cellPadding="5" cellSpacing="0">
        <thead>
          <tr>
            <th>Time (bucket)</th>
            <th>Open</th>
            <th>High</th>
            <th>Low</th>
            <th>Close</th>
            <th>Volume</th>
            <th>Avg Sentiment</th>
            <th>Article Count</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={row.time}>
              <td>{new Date(row.time).toLocaleString()}</td>
              <td>{Number(row.open).toFixed(2)}</td>
              <td>{Number(row.high).toFixed(2)}</td>
              <td>{Number(row.low).toFixed(2)}</td>
              <td>{Number(row.close).toFixed(2)}</td>
              <td>{row.volume}</td>
              <td>{row.avg_sentiment?.toFixed(3)}</td>
              <td>{row.article_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        <a href="/">Back to list</a>
      </p>
    </main>
  );
}