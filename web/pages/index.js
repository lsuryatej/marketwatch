import useSWR from 'swr';

const fetcher = (...args) => fetch(...args).then((res) => res.json());

export default function Home() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || '';
  const { data, error } = useSWR(`${apiBase}/api/v1/companies`, fetcher);
  if (error) return <div>Error fetching companies</div>;
  if (!data) return <div>Loading...</div>;
  return (
    <main style={{ padding: '2rem', fontFamily: 'Arial, sans-serif' }}>
      <h1>Market Intelligence Dashboard</h1>
      <p>Select a company to view its sentiment and price history.</p>
      <ul>
        {data.map((company) => (
          <li key={company.id}>
            <a href={`/${company.ticker.toLowerCase()}`}>{company.ticker}</a>
          </li>
        ))}
      </ul>
    </main>
  );
}