import { notFound } from "next/navigation";
import { loadType } from "@/lib/loadData";
import { copy, isLocale, labels, locales } from "@/lib/locales";

export const dynamic = "force-dynamic";
export default async function HomePage({ params }: { params: { locale: string } }) {
  if (!isLocale(params.locale)) notFound();
  const locale = params.locale, t = copy[locale];
  const source = await loadType("videos");
  const translations = locale === "ar" ? source : await loadType("videos", locale);
  const byId = new Map(translations.map(item => [String(item.id), item]));
  const videos = [...source].sort((a,b) => (a.episode_number ?? 999) - (b.episode_number ?? 999));
  return <main>
    <section className="hero"><p className="eyebrow">٤٠ · AN-NAWAWI</p><h1>{t.title}</h1><p>{t.intro}</p></section>
    <nav className="languages" aria-label="Languages">{locales.map(l => <a key={l} lang={l} dir={l === "ar" || l === "ur" ? "rtl" : "ltr"} className={l === locale ? "active" : ""} href={`/${l}`}>{labels[l]}</a>)}</nav>
    <div className="section-title"><h2>{t.lessons}</h2><span>{videos.length.toString().padStart(2,"0")}</span></div>
    {videos.length === 0 ? <p className="empty">{t.empty}</p> : <div className="lesson-list">{videos.map((video, i) => {
      const item = byId.get(String(video.id));
      return <a className="lesson-card" key={video.id} href={`/${locale}/lessons/${video.id}`}><span className="number">{String(video.episode_number ?? i+1).padStart(2,"0")}</span><div><small>{t.lesson} {video.episode_number ?? i+1}</small><h3>{item?.title || t.missing}</h3>{item?.summary && <p>{item.summary}</p>}</div><span className="arrow">{locale === "ar" || locale === "ur" ? "←" : "→"}</span></a>;
    })}</div>}
  </main>;
}
