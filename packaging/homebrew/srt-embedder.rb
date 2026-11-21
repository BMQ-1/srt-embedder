class SrtEmbedder < Formula
  include Language::Python::Virtualenv

  desc "Global CLI & Studio GUI for embedding subtitles into videos with fallbacks & sync"
  homepage "https://github.com/BMQ-1/srt-embedder"
  url "https://github.com/BMQ-1/srt-embedder/archive/refs/tags/v1.2.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.11"
  depends_on "ffmpeg"
  depends_on "mkvtoolnix"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "srt-embedder 1.2.0", shell_output("#{bin}/srt-embedder --version")
  end
end
