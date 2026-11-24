import type { Metadata } from "next";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { findItemById, loadSeo, loadTranscript } from "@/lib/loadData";
import { copy, isLocale, labels, locales } from "@/lib/locales";

export const dynamic = "force-dynamic";
export async function generateMetadata({ params }: { params: { locale: string; id: string } }): Promise<Metadata> {
  if (!isLocale(params.locale) || !/^\d+$/.test(params.id)) return {};
  const item = await findItemById("videos", params.id, params.locale === "ar" ? undefined : params.locale);
  const seo = loadSeo(params.id, params.locale);
  return { title: seo.title || item?.title, description: seo.description || item?.summary };
}
export default async function Lesson({ params }: { params: { locale: string; id: string } }) {
  if (!isLocale(params.locale) || !/^\d+$/.test(params.id)) notFound();
  const locale = params.locale, t = copy[locale];
  const source = await findItemById("videos", params.id);
  if (!source) notFound();
  const item = locale === "ar" ? source : await findItemById("videos", params.id, locale);
  const text = loadTranscript(params.id, locale === "ar" ? undefined : locale);
  // Only the validator's strict tag format is accepted; render it as a cited quotation.
  const markdown = text?.replace(/<quran\s+ref="(\d+:\d+(?:-\d+)?)"\s*>([\s\S]*?)<\/quran>/g, "\n\n> $2\n> ($1)\n\n");
  return <main><a className="back" href={`/${locale}`}>← {t.back}</a>
    <nav className="languages" aria-label="Languages">{locales.map(l => <a key={l} lang={l} dir={l === "ar" || l === "ur" ? "rtl" : "ltr"} className={l === locale ? "active" : ""} href={`/${l}/lessons/${params.id}`}>{labels[l]}</a>)}</nav>
    <article><p className="eyebrow">{t.lesson} {source.episode_number}</p><h1>{item?.title || t.missing}</h1><p className="notice">{t.review}</p>
      {markdown && item ? <div className="article-body"><ReactMarkdown>{markdown}</ReactMarkdown></div> : <p>{t.missing}</p>}
      {source.youtube_id && <a href={`https://www.youtube.com/watch?v=${encodeURIComponent(source.youtube_id)}`} target="_blank" rel="noopener noreferrer">{t.watch} ↗</a>}
    </article>
    <aside className="quran-cta"><div><h2>{t.quran}</h2><p>{t.quranText}</p></div><a href="https://qurancomplex.gov.sa/" target="_blank" rel="noopener noreferrer">{t.quran} ↗</a></aside>
  </main>;
}
